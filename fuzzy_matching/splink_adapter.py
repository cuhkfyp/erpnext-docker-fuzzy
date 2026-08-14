"""Optional local Splink/DuckDB probabilistic linkage adapter.

The import is intentionally lazy. Frappe can run deterministic evaluation even
when the optional statistical dependency has not yet been installed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any


class SplinkUnavailable(RuntimeError):
    pass


RANDOM_MATCH_PRIOR = 0.0001
MAX_DIRECT_SCORING_PAIRS = 5_000
REQUESTED_PAIR_BATCH_SIZE = 20_000
U_RANDOM_MAX_PAIRS = 1_000_000
SPLINK_ADAPTER_VERSION = "pilot-splink-1.1"
COMPARISON_FIELDS = ("chi_full", "eng_full", "birthday", "phone", "email")


@dataclass(frozen=True)
class ProbabilityPrediction:
    left_id: str
    right_id: str
    probability: float
    match_weight: float | None = None


def _null_missing_comparison_values(
    records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Represent missing comparison evidence as null, never as exact empty text."""
    output = []
    for record in records:
        row = dict(record)
        for fieldname in COMPARISON_FIELDS:
            if row.get(fieldname) == "":
                row[fieldname] = None
        output.append(row)
    return output


def available() -> bool:
    try:
        import duckdb
        import splink

        return True
    except Exception:
        return False


def dependency_versions() -> dict[str, str]:
    output = {}
    for package in ("splink", "duckdb"):
        try:
            output[package] = version(package)
        except PackageNotFoundError:
            output[package] = "unavailable"
    return output


def fit_predict(
    records: Iterable[dict[str, Any]],
    *,
    max_block_size: int = 10_000,
    max_prediction_pairs: int = 500_000,
    requested_pairs: Iterable[tuple[str, str]] | None = None,
    scoring_records: Iterable[dict[str, Any]] | None = None,
    batch_requested_pairs: bool = False,
    requested_min_probability: float | None = None,
    u_random_max_pairs: int = U_RANDOM_MAX_PAIRS,
) -> list[ProbabilityPrediction]:
    """Train an unsupervised link-only model and return local predictions.

    Input rows must contain `record_id`, `source`, and the canonical fields used
    below. The function performs no network calls and does not persist raw data.
    """
    try:
        import pandas as pd
        import splink.comparison_library as cl
        from splink import DuckDBAPI, Linker, SettingsCreator, block_on
    except Exception as exc:
        raise SplinkUnavailable(
            "Install the pinned splink and duckdb dependencies in the ERPNext worker environment"
        ) from exc

    # Canonical normalization uses an empty string to mean unavailable
    # evidence. Splink comparison levels require a real null for that state;
    # leaving empty strings in place makes two missing values look like an
    # exact agreement and corrupts the learned m/u probabilities.
    rows = _null_missing_comparison_values(records)
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    required = {"record_id", "source"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Splink input is missing required columns: {', '.join(missing)}")

    comparisons = []
    if "chi_full" in frame:
        comparisons.append(cl.JaroWinklerAtThresholds("chi_full", [0.95, 0.85]))
    if "eng_full" in frame:
        comparisons.append(
            cl.JaroWinklerAtThresholds("eng_full", [0.95, 0.88, 0.80]).configure(
                term_frequency_adjustments=True
            )
        )
    if "birthday" in frame:
        comparisons.append(cl.ExactMatch("birthday"))
    if "phone" in frame:
        comparisons.append(cl.ExactMatch("phone"))
    if "email" in frame:
        comparisons.append(cl.ExactMatch("email").configure(term_frequency_adjustments=True))
    if not comparisons:
        raise ValueError("Splink requires at least one configured comparison column")

    route_values = {}
    for fieldname in ("global_id", "phone", "email", "chi_full"):
        if fieldname in frame:
            route_values[fieldname] = frame[fieldname].fillna("").astype(str)
    if "birthday" in frame and "surname_key" in frame:
        route_values["dob_surname"] = (
            frame["birthday"].fillna("").astype(str)
            + "|"
            + frame["surname_key"].fillna("").astype(str)
        ).where(frame["birthday"].fillna("").astype(str).ne(""), "")
    if "eng_surname" in frame and "eng_given_prefix" in frame:
        route_values["eng_name"] = (
            frame["eng_surname"].fillna("").astype(str)
            + "|"
            + frame["eng_given_prefix"].fillna("").astype(str)
        ).where(frame["eng_given_prefix"].fillna("").astype(str).ne(""), "")

    # Convert each route to an opaque working column and discard block values
    # that are too large. The conservative projected-pair budget counts
    # duplicates across routes, so actual generated pairs cannot exceed it.
    blocking_rules = []
    remaining_pairs = max(1, int(max_prediction_pairs))
    for index, (_route, values) in enumerate(route_values.items()):
        counts_frame = pd.DataFrame({"value": values, "source": frame["source"].astype(str)})
        counts_frame = counts_frame.loc[counts_frame["value"].ne("")]
        allowed = set()
        for value, group in counts_frame.groupby("value", sort=True):
            source_counts = group.groupby("source").size().tolist()
            record_count = sum(source_counts)
            projected_pairs = sum(
                left_count * right_count
                for left_index, left_count in enumerate(source_counts)
                for right_count in source_counts[left_index + 1 :]
            )
            if (
                projected_pairs
                and record_count <= max(2, int(max_block_size))
                and projected_pairs <= remaining_pairs
            ):
                allowed.add(value)
                remaining_pairs -= projected_pairs
        if allowed:
            column = f"__block_{index}"
            frame[column] = values.where(values.isin(allowed), "")
            blocking_rules.append(block_on(column))
        if remaining_pairs <= 0:
            break
    if not blocking_rules:
        raise ValueError("No Splink blocks fit within the configured candidate safeguards")

    sources = sorted(frame["source"].fillna("").astype(str).unique())
    sources = [source for source in sources if source]
    if len(sources) < 2:
        raise ValueError("Splink link-only evaluation requires at least two record sources")

    settings = SettingsCreator(
        link_type="link_only",
        unique_id_column_name="record_id",
        comparisons=comparisons,
        blocking_rules_to_generate_predictions=blocking_rules,
        # Exact names in CCD can be common or incomplete, so they are not a
        # sufficiently precise deterministic rule for estimating the random
        # match prior. Use Splink's conservative default explicitly and let
        # governed human labels calibrate the deployable score thresholds.
        probability_two_random_records_match=RANDOM_MATCH_PRIOR,
        retain_matching_columns=True,
        retain_intermediate_calculation_columns=True,
    )
    # Splink's link-only mode expects one input table per source.  Supplying a
    # single frame with a source column appears valid but causes its training
    # SQL to reference a synthetic ``source_dataset`` column that is absent.
    # Separate frames plus explicit aliases make Splink create that column and
    # also guarantee that it predicts cross-source pairs only.
    source_frames = [frame.loc[frame["source"].astype(str) == source].copy() for source in sources]
    # Splink uses aliases as DuckDB table identifiers in generated SQL. CCD
    # Registration names may contain hyphens, spaces, or other punctuation, so
    # never pass the business-facing source name through as an SQL identifier.
    # Positional aliases are private working names; the original source remains
    # available in the input column for policy logic and audit output.
    source_aliases = [f"source_{index}" for index in range(len(source_frames))]
    linker = Linker(
        source_frames,
        settings,
        db_api=DuckDBAPI(),
        input_table_aliases=source_aliases,
    )
    linker.training.estimate_u_using_random_sampling(
        max_pairs=max(1, int(u_random_max_pairs)),
        seed=0,
    )
    for rule in blocking_rules[:3]:
        try:
            linker.training.estimate_parameters_using_expectation_maximisation(rule)
        except Exception:
            continue
    predictions = (
        None
        if batch_requested_pairs
        else linker.inference.predict(threshold_match_weight=-20).as_pandas_dataframe()
    )

    output: list[ProbabilityPrediction] = []
    predicted_keys: set[tuple[str, str]] = set()
    for row in predictions.to_dict("records") if predictions is not None else []:
        left = str(row.get("record_id_l") or row.get("unique_id_l") or "")
        right = str(row.get("record_id_r") or row.get("unique_id_r") or "")
        if not left or not right:
            continue
        predicted_keys.add(tuple(sorted((left, right))))
        output.append(
            ProbabilityPrediction(
                left,
                right,
                float(row["match_probability"]),
                float(row["match_weight"]) if row.get("match_weight") is not None else None,
            )
        )

    # Population inference remains restricted to the safeguarded blocking
    # rules above. For a small, already-selected human-review set, score any
    # missing pairs directly with the trained model so the statistical model
    # can be calibrated on the same governed labels. This never creates new
    # production candidates and is bounded by the caller's review sample.
    scoring_rows = _null_missing_comparison_values(
        scoring_records if scoring_records is not None else frame.to_dict("records")
    )
    # The trained settings retain opaque working block columns for diagnostics.
    # Requested-pair scoring does not use them for joining, but Splink still
    # selects them in its comparison pipeline, so supply harmless empty values.
    working_block_columns = [
        str(column) for column in frame.columns if str(column).startswith("__block_")
    ]
    for row in scoring_rows:
        for column in working_block_columns:
            row.setdefault(column, "")
    row_by_id = {
        str(row.get("record_id") or ""): row
        for row in scoring_rows
        if row.get("record_id")
    }
    direct_pairs = {
        tuple(sorted((str(left), str(right))))
        for left, right in (requested_pairs or ())
        if left and right and left != right
    }
    missing_requested = sorted(direct_pairs - predicted_keys)
    if batch_requested_pairs:
        output.extend(
            _batch_score_requested_pairs(
                linker,
                row_by_id,
                missing_requested,
                minimum_probability=requested_min_probability,
            )
        )
        return output

    for left, right in missing_requested[:MAX_DIRECT_SCORING_PAIRS]:
        left_row = row_by_id.get(left)
        right_row = row_by_id.get(right)
        if not left_row or not right_row:
            continue
        try:
            direct = linker.inference.compare_two_records(left_row, right_row).as_pandas_dataframe()
            if direct.empty:
                continue
            prediction = direct.iloc[0]
            output.append(
                ProbabilityPrediction(
                    left,
                    right,
                    float(prediction["match_probability"]),
                    (
                        float(prediction["match_weight"])
                        if prediction.get("match_weight") is not None
                        else None
                    ),
                )
            )
        except Exception:
            # A single sparse/malformed pair must not discard the trained
            # model's valid predictions for the remainder of the review set.
            continue
    return output


def _batch_score_requested_pairs(
    linker: Any,
    row_by_id: dict[str, dict[str, Any]],
    requested_pairs: list[tuple[str, str]],
    *,
    minimum_probability: float | None = None,
    batch_size: int = REQUESTED_PAIR_BATCH_SIZE,
) -> list[ProbabilityPrediction]:
    """Score exact requested pairs in bounded SQL batches with one trained model.

    Splink's public ``compare_two_records`` method creates a Cartesian product
    when given multiple rows. This pinned-version adapter uses the same
    comparison/TF pipeline but joins the two temporary inputs on an opaque pair
    sequence. Every requested pair must yield exactly one score before threshold
    filtering; otherwise generation fails closed instead of publishing a partial
    Review queue.
    """
    try:
        import pandas as pd
        from splink.internals.find_matches_to_new_records import (
            add_unique_id_and_source_dataset_cols_if_needed,
        )
        from splink.internals.misc import ascii_uid
        from splink.internals.pipeline import CTEPipeline
        from splink.internals.predict import (
            predict_from_comparison_vectors_sqls_using_settings,
        )
        from splink.internals.term_frequencies import (
            _join_new_table_to_df_concat_with_tf_sql,
            colname_to_tf_tablename,
        )
    except Exception as exc:
        raise SplinkUnavailable(
            "The pinned Splink batch-scoring internals are unavailable"
        ) from exc

    output: list[ProbabilityPrediction] = []
    for start in range(0, len(requested_pairs), max(1, int(batch_size))):
        batch = requested_pairs[start : start + max(1, int(batch_size))]
        left_rows = []
        right_rows = []
        pair_by_sequence: dict[str, tuple[str, str]] = {}
        for offset, (left_id, right_id) in enumerate(batch):
            left = row_by_id.get(left_id)
            right = row_by_id.get(right_id)
            if left is None or right is None:
                raise ValueError("A requested Splink pair references a missing record")
            sequence = str(start + offset)
            pair_by_sequence[sequence] = (left_id, right_id)
            left_row = dict(left)
            right_row = dict(right)
            left_row["record_id"] = f"{sequence}:L"
            right_row["record_id"] = f"{sequence}:R"
            left_row["__pair_sequence"] = sequence
            right_row["__pair_sequence"] = sequence
            left_rows.append(left_row)
            right_rows.append(right_row)

        uid = ascii_uid(8)
        left_table = linker.table_management.register_table(
            pd.DataFrame(left_rows),
            f"__splink__requested_left_{uid}",
            overwrite=True,
        )
        right_table = linker.table_management.register_table(
            pd.DataFrame(right_rows),
            f"__splink__requested_right_{uid}",
            overwrite=True,
        )
        left_table.templated_name = "__splink__requested_left"
        right_table.templated_name = "__splink__requested_right"
        pipeline = CTEPipeline([left_table, right_table])
        cache = linker._intermediate_table_cache
        if "__splink__df_concat_with_tf" in cache:
            pipeline.append_input_dataframe(
                cache.get_with_logging("__splink__df_concat_with_tf")
            )
        for tf_col in linker._settings_obj._term_frequency_columns:
            table_name = colname_to_tf_tablename(tf_col)
            if table_name in cache:
                pipeline.append_input_dataframe(cache.get_with_logging(table_name))

        pipeline.enqueue_sql(
            _join_new_table_to_df_concat_with_tf_sql(
                linker, "__splink__requested_left", left_table
            ),
            "__splink__requested_left_with_tf",
        )
        pipeline.enqueue_sql(
            _join_new_table_to_df_concat_with_tf_sql(
                linker, "__splink__requested_right", right_table
            ),
            "__splink__requested_right_with_tf",
        )
        pipeline = add_unique_id_and_source_dataset_cols_if_needed(
            linker,
            left_table,
            pipeline,
            in_tablename="__splink__requested_left_with_tf",
            out_tablename="__splink__requested_left_with_tf_uid_fix",
            uid_str="_left",
        )
        pipeline = add_unique_id_and_source_dataset_cols_if_needed(
            linker,
            right_table,
            pipeline,
            in_tablename="__splink__requested_right_with_tf",
            out_tablename="__splink__requested_right_with_tf_uid_fix",
            uid_str="_right",
        )
        select_expr = ", ".join(
            linker._settings_obj._columns_to_select_for_blocking
        )
        pipeline.enqueue_sql(
            f"""SELECT {select_expr}, 0 AS match_key
                FROM __splink__requested_left_with_tf_uid_fix AS l
                INNER JOIN __splink__requested_right_with_tf_uid_fix AS r
                    ON l.__pair_sequence = r.__pair_sequence""",
            "__splink__compare_two_records_blocked",
        )
        select_expr = ", ".join(
            linker._settings_obj._columns_to_select_for_comparison_vector_values
        )
        pipeline.enqueue_sql(
            f"""SELECT {select_expr}
                FROM __splink__compare_two_records_blocked""",
            "__splink__df_comparison_vectors",
        )
        pipeline.enqueue_list_of_sqls(
            predict_from_comparison_vectors_sqls_using_settings(
                linker._settings_obj,
                sql_infinity_expression=linker._infinity_expression,
            )
        )
        prediction_table = linker._db_api.sql_pipeline_to_splink_dataframe(
            pipeline, use_cache=False
        )
        prediction_frame = prediction_table.as_pandas_dataframe()
        if len(prediction_frame) != len(batch):
            raise ValueError(
                "Splink batch scoring did not return exactly one score per requested pair"
            )
        seen_sequences = set()
        for row in prediction_frame.to_dict("records"):
            encoded_left = str(row.get("record_id_l") or row.get("unique_id_l") or "")
            encoded_right = str(row.get("record_id_r") or row.get("unique_id_r") or "")
            sequence = encoded_left.split(":", 1)[0]
            if not sequence or encoded_right.split(":", 1)[0] != sequence:
                raise ValueError("Splink batch output lost its requested-pair sequence")
            pair = pair_by_sequence.get(sequence)
            if pair is None or sequence in seen_sequences:
                raise ValueError("Splink batch output contains an unknown or duplicate pair")
            seen_sequences.add(sequence)
            probability = float(row["match_probability"])
            if minimum_probability is None or probability >= minimum_probability:
                output.append(
                    ProbabilityPrediction(
                        pair[0],
                        pair[1],
                        probability,
                        (
                            float(row["match_weight"])
                            if row.get("match_weight") is not None
                            else None
                        ),
                    )
                )
        if len(seen_sequences) != len(batch):
            raise ValueError("Splink batch scoring omitted a requested pair")
        prediction_table.drop_table_from_database_and_remove_from_cache()
        left_table.drop_table_from_database_and_remove_from_cache(
            force_non_splink_table=True
        )
        right_table.drop_table_from_database_and_remove_from_cache(
            force_non_splink_table=True
        )
    return output


def score_requested_pairs(
    training_records: Iterable[dict[str, Any]],
    scoring_records: Iterable[dict[str, Any]],
    requested_pairs: Iterable[tuple[str, str]],
    *,
    minimum_probability: float,
    max_block_size: int = 10_000,
    max_prediction_pairs: int = 500_000,
    u_random_max_pairs: int = U_RANDOM_MAX_PAIRS,
) -> list[ProbabilityPrediction]:
    """Train once and return only requested pairs at/above a governed cutoff."""
    requested = {
        tuple(sorted((str(left), str(right))))
        for left, right in requested_pairs
        if left and right and left != right
    }
    if not requested:
        return []
    predictions = fit_predict(
        training_records,
        max_block_size=max_block_size,
        max_prediction_pairs=max_prediction_pairs,
        requested_pairs=requested,
        scoring_records=scoring_records,
        batch_requested_pairs=True,
        requested_min_probability=float(minimum_probability),
        u_random_max_pairs=u_random_max_pairs,
    )
    output = {}
    for prediction in predictions:
        key = tuple(sorted((prediction.left_id, prediction.right_id)))
        if key in requested and prediction.probability >= minimum_probability:
            output[key] = prediction
    return [output[key] for key in sorted(output)]
