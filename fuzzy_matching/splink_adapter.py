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


@dataclass(frozen=True)
class ProbabilityPrediction:
    left_id: str
    right_id: str
    probability: float
    match_weight: float | None = None


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

    rows = list(records)
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
    linker.training.estimate_u_using_random_sampling(max_pairs=1_000_000, seed=0)
    for rule in blocking_rules[:3]:
        try:
            linker.training.estimate_parameters_using_expectation_maximisation(rule)
        except Exception:
            continue
    predictions = linker.inference.predict(threshold_match_weight=-20).as_pandas_dataframe()

    output: list[ProbabilityPrediction] = []
    predicted_keys: set[tuple[str, str]] = set()
    for row in predictions.to_dict("records"):
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
    row_by_id = {
        str(row.get("record_id") or ""): row
        for row in frame.to_dict("records")
        if row.get("record_id")
    }
    direct_pairs = {
        tuple(sorted((str(left), str(right))))
        for left, right in (requested_pairs or ())
        if left and right and left != right
    }
    for left, right in sorted(direct_pairs - predicted_keys)[:MAX_DIRECT_SCORING_PAIRS]:
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
