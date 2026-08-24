# ERPNext Server Migration and Transfer Runbook

## Document control

| Item | Value |
| --- | --- |
| Purpose | Move the guarded CCD matching and identity-resolution setup to another ERPNext server |
| Runbook date | 2026-08-24 UTC |
| Implementation code checkpoint | The reviewed Git commit containing this runbook, or a reviewed successor |
| Source site at writing | `frontend` |
| Source framework baseline | Frappe 15.73.0 / ERPNext 15.70.0 |
| Required activation state during transfer | Live materialization disabled |
| Includes | Feature-only installation, full-site transfer, complete matching-evidence rebuild, validation, cutover, rollback, and new-centre guidance |
| Does not authorize | Identity activation, destructive CCD merge, or deletion of audit history |

This runbook is the authoritative portability guide for the identity-resolution
component. Migration and identity activation are deliberately separate:

```text
Migration
  code + app dependencies + schema + site data + validation
  materialization remains OFF

Activation
  backup + bounded batch + explicit approval + Apply + post-write checks
  performed only after separate authorization
```

## 1. Choose the transfer mode

Use exactly one primary mode.

| Mode | Use when | Moves existing recommendations/reviews/groups? |
| --- | --- | --- |
| A. Full-site lift-and-shift | The new server replaces the current ERPNext site | Yes; database restore preserves the whole site state |
| B. Feature-only installation | The target already has its own compatible CCD data/site | No; deploy code/schema and generate target-specific evaluations/canaries |
| C. Same-layout managed Docker recovery | Rebuilding this exact Docker host layout from its persistent app copy and site backup | Yes if the database/files are restored |

Do not copy recommendation, review, or identity tables selectively between
unrelated sites. Their record names, policy snapshots, source timestamps,
fingerprints, decisions, and audit links are site-specific.

## 2. Understand the application boundary

The GitHub repository is not a standalone Frappe app checkout. Its root maps to
the Python package directory:

```text
GitHub repository root
  → frappe-bench/apps/db_connector/db_connector/
```

The complete private Frappe app also needs its parent-level files, including:

```text
frappe-bench/apps/db_connector/pyproject.toml
frappe-bench/apps/db_connector/db_connector/modules.txt
frappe-bench/apps/db_connector/db_connector/hooks.py
private db_connector modules not published in the component repository
```

The current host keeps a complete persistent copy at:

```text
/root/erpnext_docker_volume/persistent_apps/db_connector
```

The matching component also depends on the site-specific app that owns:

- `CCD Master`;
- `CCD Registration` and its governed field mapping;
- the existing source fields such as `ccd_reg_source`; and
- any private business logic referenced by the target's `db_connector/hooks.py`.

On the current site that owner is the private `hksr` app. A full-site transfer
must install every app reported by `bench --site <site> list-apps`, at compatible
versions, before reopening the restored site. The current site's broader app
list is not a promise that every app is required for a feature-only target, but
it is required for an exact full-site restore unless an app-removal migration
has been separately designed and tested.

### 2.1 CCD Master identity-view loading boundary

`CCD Master` is a custom DocType (`tabDocType.custom = 1`) on the current site.
Frappe intentionally skips `doctype_js` hooks while building form metadata for
a custom DocType. Therefore, the existence of this hook alone is **not** proof
that the Identity Resolution tab can render:

```python
doctype_js = {
    "CCD Master": "public/js/ccd_master_identity_resolution.js",
}
```

`db_connector.identity_resolution_setup.install_identity_resolution` reads that
versioned JavaScript and idempotently creates or updates an enabled Form Client
Script named **CCD Master Identity Resolution** when `CCD Master` is custom. If
a future target uses a standard `CCD Master`, the managed Client Script is kept
disabled and the normal `doctype_js` hook supplies the same renderer, avoiding
duplicate form handlers.

The tab's HTML is dynamic. No group/member summary is stored in the CCD Master
document itself: the form script calls
`db_connector.api_identity_resolution.get_identity_resolution` and renders the
response. A visible but empty tab means the renderer did not load or execute;
it does not prove that Membership rows are absent.

## 3. Required artifacts

### 3.1 Code and manifests

Collect and checksum:

1. the complete private `db_connector` app tree;
2. the complete app that owns `CCD Master`/`CCD Registration`;
3. every private or public app installed on the source site for a full restore;
4. the reviewed matching component commit;
5. `bench version` output;
6. `bench --site <site> list-apps` output; and
7. the target's container/Bench topology and process names.

The component commit can be verified without exposing client data:

```bash
git ls-remote https://github.com/cuhkfyp/erpnext-docker-fuzzy.git refs/heads/main
```

Do not commit site backups, `site_config.json`, database credentials, encryption
keys, exports, logs, model databases, or client identity data to Git.

### 3.2 Site state for a full transfer

Collect:

- compressed database backup;
- public files archive;
- private files archive;
- the source site's configuration backup;
- the source site's encryption key, transferred separately and securely; and
- checksums for every artifact.

The encryption key is required to decrypt stored Password fields after restore.
Treat it as a secret; do not paste it into this repository or ordinary tickets.

## 4. Backup and backup cadence

### 4.1 Before migration or activation

For the current Docker site, the supported full backup command is:

```bash
docker exec frappe_docker-backend-1 \
  bench --site frontend backup --with-files --compress --verbose
```

For a conventional Bench:

```bash
cd /home/frappe/frappe-bench
bench --site <site> backup --with-files --compress --verbose
```

After backup:

1. identify the database, public-file, private-file, and configuration outputs;
2. copy them to storage independent of the application container/host;
3. calculate `sha256sum` for each output;
4. verify that the files are non-empty and readable; and
5. periodically prove restoration on a non-production environment.

Git protects source history. It does not back up ERPNext data or files.

### 4.2 When a new centre is added

Creating one `CCD Registration` does not by itself require a special one-off
backup if the normal ERPNext backup schedule is current and verified. Take a
fresh explicit backup before any higher-risk boundary:

- bulk-importing a centre's CCD records;
- changing governed field mappings or identifier scope;
- migrating schema/app versions;
- generating and approving a new production canary; or
- applying any material identity activation wave.

Likewise, an ordinary single `CCD Master` insert does not require its own full
backup. Protect normal day-to-day changes with the verified scheduled backup;
take an explicit checkpoint before a bulk import, schema/policy change, or
activation Apply. Merely turning Materialization on or off writes no Identity
Group or Membership, so the write boundary that needs the checkpoint is
**Apply Approved Batch** (or a human finalization that materializes while the
global switch is on), not the switch by itself.

A new centre changes the matching population and usually the policy/source
profile. Do not add it to an old frozen canary. Create a new policy version or
reviewed source-profile snapshot, rerun the required evaluation/validation, and
generate a new canary that includes the new centre.

## 5. Pre-transfer safety state

Before either transfer mode:

1. record the current Identity Resolution Settings and live object counts;
2. set `materialization_enabled = 0`;
3. keep the site in maintenance mode or stop user/background writes during the
   final full-site backup;
4. finish or deliberately stop long-running queue jobs;
5. do not run source and target as writable production sites simultaneously;
6. record the exact component commit and full private-app source revision; and
7. verify a rollback owner and cutover decision-maker are available.

`automation_paused = 0` means no QC circuit breaker is currently tripped. It
does not disable the circuit-breaker feature. During transfer, the global
`materialization_enabled = 0` switch is the primary fail-closed control.

## 6. Mode A — full-site lift-and-shift

Use this mode when the target will replace the current site and must preserve
the current 3,961 recommendations, 11,177 review candidates, reviews, settings,
and any identity history created in the future.

### 6.1 Inventory the source

On the source Bench, save sanitized manifests outside the public web root:

```bash
cd /home/frappe/frappe-bench
bench version > /secure-transfer/source-bench-versions.txt
bench --site frontend list-apps > /secure-transfer/source-site-apps.txt
```

Record database engine/version, Python version, Node/Yarn versions, operating
system/image versions, worker queues, scheduled jobs, and reverse-proxy settings.

### 6.2 Freeze writes and back up

1. announce the maintenance window;
2. enable ERPNext maintenance mode or stop inbound traffic;
3. stop scheduler/queue workers after current jobs finish;
4. verify live identity materialization is disabled;
5. run the full backup from section 4; and
6. checksum and transfer the artifacts securely.

If the source already has live Identity Memberships at a later date, this
freeze prevents the source and target from diverging during cutover.

### 6.3 Prepare the target software

Before restoring the database:

1. install compatible Frappe/ERPNext versions;
2. install every source-site app at its reviewed compatible revision;
3. place the complete private `db_connector` app at
   `frappe-bench/apps/db_connector`;
4. place the complete `hksr`/CCD-owner app at its expected app path;
5. overlay the reviewed matching component into
   `frappe-bench/apps/db_connector/db_connector`;
6. install the pinned Python requirements in every runtime that imports the
   app; and
7. keep web, scheduler, and workers stopped until restore/migration finishes.

Do not treat a successful Python import in only the web container as complete.
Backend, scheduler, long queue, and short queue must load the same app code and
dependencies.

Use the non-destructive staging method in section 7.2 for the component
overlay, and merge private hooks rather than replacing unrelated integrations.

### 6.4 Restore database and files

Create or select the target site, then restore with paths appropriate to the
transferred backup:

```bash
cd /home/frappe/frappe-bench
bench --site <target-site> restore /secure-transfer/<database>.sql.gz \
  --with-public-files /secure-transfer/<public-files>.tar \
  --with-private-files /secure-transfer/<private-files>.tar
```

Supply the database root credentials through the target's approved secret
mechanism when Bench requests them. Restore/copy the source encryption key
securely into the target site configuration before users test encrypted fields.

Avoid `--force` unless a separately reviewed incompatibility decision explains
why validation and downgrade warnings can be ignored.

### 6.5 Migrate and rebuild

With the complete app code present:

```bash
cd /home/frappe/frappe-bench
./env/bin/pip install -r apps/db_connector/db_connector/requirements.txt
bench --site <target-site> migrate
bench --site <target-site> execute db_connector.api_fuzzy_evaluation.install_matching_roles
bench --site <target-site> execute db_connector.api_fuzzy_evaluation.install_default_pilot_policy
bench --site <target-site> execute db_connector.api_fuzzy_canary.install_existing_canary_review_workflows
bench --site <target-site> execute db_connector.identity_resolution_setup.install_identity_resolution
bench --site <target-site> execute db_connector.identity_snapshot_backfill.preview_legacy_identity_fingerprint_backfill
bench --site <target-site> execute db_connector.identity_snapshot_backfill.apply_legacy_identity_fingerprint_backfill
bench --site <target-site> build --app db_connector
bench --site <target-site> clear-cache
```

The two fingerprint-backfill commands are idempotent migration safeguards. The
first is zero-write. The second requires Materialization to be off, locks the
participating CCD records, and fills only missing snapshot fingerprints whose
current source and `modified` timestamp still exactly match the frozen row under
its verified policy snapshot. It creates no Identity Decision, Group,
Membership, or Exclusion. Any stale, missing, corrupt-policy, or inconsistent
row is left untouched and reported for a new canary/queue instead of being
silently repaired. On a fresh target with no legacy snapshots, both commands
report zero rows.

Restart the web, scheduler, long-queue, and short-queue processes using the
target's service manager. Run `bench --site <target-site> doctor` afterward.

### 6.6 Keep activation off before opening traffic

Before taking the target out of maintenance mode, verify in
`CCD Identity Resolution Settings`:

```text
materialization_enabled = 0
```

If the source backup was taken after a future activation and intentionally
contains live groups, those groups will be visible after restore, but new links
must still remain disabled until target validation and cutover approval finish.

### 6.7 Cut over

1. run all validation in section 9;
2. confirm no writes occurred after the source freeze;
3. change DNS/proxy routing to the target;
4. open the target to users;
5. keep the old site read-only for the agreed rollback window; and
6. never allow both copies to accept identity decisions concurrently.

## 7. Mode B — install the feature on an existing target site

Use this mode when the target has its own CCD population. Do not restore the
source matching tables into that target.

### 7.1 Target prerequisites

Confirm:

- compatible Frappe/ERPNext versions;
- a complete, installed `db_connector` Frappe app;
- the `CCD Master` and `CCD Registration` DocTypes;
- required source/key fields used by the matching policy;
- backend, scheduler, and queue processes that share the same Python app;
- sufficient worker memory/disk for the approved candidate generation; and
- System Manager, Reviewer, and Sensitive Reviewer governance assignments.

If the target's `hooks.py` has different private integrations, merge the
identity hooks instead of blindly overwriting it. Required integrations are:

- `after_migrate = db_connector.identity_resolution_setup.after_migrate`;
- the CCD Master `doctype_js` source file plus the managed **CCD Master Identity
  Resolution** Client Script installed by `install_identity_resolution` when
  CCD Master is custom;
- CCD Master `on_update` event for fingerprint revalidation; and
- daily `db_connector.api_identity_qc.run_qc_monitor` scheduling.

Preserve every unrelated target hook.

### 7.2 Stage the component safely

Clone into a temporary staging directory, not directly over the app:

```bash
transfer_stage="$(mktemp -d)"
git clone https://github.com/cuhkfyp/erpnext-docker-fuzzy.git \
  "$transfer_stage/erpnext-docker-fuzzy"
git -C "$transfer_stage/erpnext-docker-fuzzy" checkout a0ae535
mkdir -p "$transfer_stage/component"
git -C "$transfer_stage/erpnext-docker-fuzzy" archive a0ae535 \
  | tar -x -C "$transfer_stage/component" -f -
```

Review the staged files and merge them into:

```text
/home/frappe/frappe-bench/apps/db_connector/db_connector/
```

Do not use a destructive `rsync --delete`: the repository intentionally does
not contain every private module in the complete app. Do not copy the staging
repository's `.git` directory into the Frappe app.

### 7.3 Install and migrate

Take a target backup first, keep materialization disabled, then run the commands
from section 6.5. For Docker or Kubernetes, install/copy the same code and
dependencies into every web/scheduler/worker image or mounted runtime before
restarting them.

### 7.4 Build target-specific matching evidence

Do not reuse the source canary on different data. On the target:

1. create/import governed source profiles from its CCD Registrations;
2. confirm identifier scope and source mappings;
3. run target-specific evaluation and independent review;
4. approve the required High Tier Validation and Threshold Evaluation;
5. promote the reviewed policy to Pilot;
6. generate a target canary;
7. run zero-write preview; and
8. seek separate activation authorization.

The following sections provide the complete procedure. Tiered Gated does not
have a statistical training step. It applies the frozen deterministic policy
and safety gates when the canary is generated. Splink fitting is automatic
inside the Threshold Evaluation and optional Review Queue jobs; there is no
portable model file or separate `train-splink` command.

### 7.5 Decide whether to preserve or rebuild matching evidence

| Transfer condition | Required action |
| --- | --- |
| Exact full-site restore with the same database, files, app revisions, policy, and CCD population | Preserve the restored evaluations, labels, canary, recommendations, and optional Review Queue; verify them rather than retraining merely because the host changed |
| Feature-only installation into a site with its own CCD population | Build all policy/evaluation/canary evidence on the target; do not import source-site matching rows |
| New centre, material population import, source mapping change, normalization/comparator change, Splink adapter change, or policy change | Create a new Draft policy/snapshot and repeat evaluation, validation, approval, canary, and any optional Review Queue |
| Existing frozen canary whose CCD records changed after its snapshot | Treat it as stale; generate a new governed canary instead of editing the old snapshot |

A server move by itself does not improve or invalidate the statistical model.
The deciding factor is whether the governed data, code, model definitions, and
policy snapshot remain identical.

### 7.6 Install roles, settings, dependencies, and a Draft policy

Before creating evidence, confirm that every web and worker runtime imports the
pinned dependencies and the long queue is healthy. The important frozen
implementation values at this checkpoint are:

| Setting | Checkpoint value | Meaning |
| --- | --- | --- |
| Policy version | `pilot-1.6` | Current approved deterministic policy code/configuration |
| Splink adapter | `pilot-splink-1.1` | Comparison definitions and fitting implementation |
| Maximum Splink training records | 5,000 | Bounded fitting cohort, not the scored population |
| Review scoring batch | 20,000 requested pairs | Bounded DuckDB scoring batch |
| Random-match prior | 0.0001 | Splink prior frozen by the adapter |
| Current Review cutoff | `0.938995074` | Review-priority cutoff for the approved snapshot only; never an automatic Same threshold |
| Materialization | Off | Must remain off throughout evidence generation and review |
| Automation paused | Normally 0 | Zero means the QC circuit breaker has not tripped |

Create the roles and default Draft policy idempotently when this is a fresh
feature installation:

```bash
cd /home/frappe/frappe-bench
bench --site <target-site> execute db_connector.api_fuzzy_evaluation.install_matching_roles
bench --site <target-site> execute db_connector.api_fuzzy_evaluation.install_default_pilot_policy
```

The required human roles are:

- `System Manager` for policy management, approvals, canary/batch creation,
  materialization settings, Apply, and adjudication;
- `CCD Match Reviewer` for masked pair/QC review; and
- `CCD Match Sensitive Reviewer` when full permitted evidence and CCD links are
  operationally required.

`Administrator` normally resolves to System Manager, but a custom role named
`Sys Admin` is not accepted by these APIs unless that user also has the exact
`System Manager` role.

In **CCD Identity Resolution Settings**, keep:

```text
Live Identity Materialization Enabled = off
New Automatic Materialization Paused  = off, unless an investigation requires pause
```

The pilot-wave, holdout, QC cadence, QC window, SLA, and review-batch fields are
workflow defaults. They do not train either model and do not automatically
select or assign records.

### 7.7 Import and govern CCD source mappings

Open the Draft **CCD Matching Policy** and use **Import Registration Mappings**,
or call the manager-protected method:

```bash
bench --site <target-site> execute db_connector.api_fuzzy_evaluation.sync_policy_source_profiles \
  --kwargs '{"policy_name":"<draft-policy>"}'
```

This replaces the Draft policy's source-profile child rows using the live
`CCD Registration.fieldmatch` mappings for sources present in `CCD Master`.
Review rather than merely accepting the import:

1. every governed `ccd_reg_source` has an existing CCD Registration;
2. canonical name, phone, email, birthday, HKSR number, and HKID mappings point
   to the intended CCD Master fields;
3. fields absent from a source are disabled rather than guessed;
4. identifier scope is explicitly governed;
5. only complete, check-digit-valid HKID values can use the current global-ID
   path; and
6. no operational/non-identity field has accidentally entered the policy.

Source mappings can be synchronized only while the policy is Draft. If the
population or mappings changed after promotion, create a new version instead
of mutating the already evaluated Pilot policy.

### 7.8 Run, review, and approve the required evaluations

Two approved evaluation purposes with the exact same policy snapshot are
mandatory before a canary can start.

#### A. Threshold Evaluation — Splink calibration and held-out validation

Start a representative run from **CCD Matching Policy → Start Shadow
Evaluation**, normally with sample size 500 and double-review count 100, or:

```bash
bench --site <target-site> execute db_connector.api_fuzzy_evaluation.install_evaluation_run \
  --kwargs '{"policy_name":"<draft-policy>","sample_size":500,"double_review_count":100}'
```

The long-queue job profiles the frozen governed population, generates candidate
pairs, selects the evaluation sample, fits Splink on a deterministic bounded
cohort of at most 5,000 records, and scores the sample. Fitting estimates the
Splink m/u probabilities and term frequencies; it does not create Identity
Groups or modify legacy matching fields.

Review every non-stale evaluation pair as Same, Different, or Unsure. Same
requires two distinct reviewers. Random double-review cases require their
independent reviews, and disagreements/Unsure require System Manager
adjudication. Then:

1. click **Finalize Evaluation**;
2. inspect calibration and held-out metrics, warnings, source-pair coverage,
   candidate truncation, skipped blocks, and adapter version; and
3. record **Management Decision → Approved** only if the evidence is accepted.

The current approved Splink result supplies a Review-priority cutoff only. It
did not validate a probabilistic automatic High threshold.

#### B. High Tier Validation — deterministic Tiered Gated precision

Start a fresh High-only validation sample:

```bash
bench --site <target-site> execute db_connector.api_fuzzy_evaluation.install_high_tier_validation_run \
  --kwargs '{"policy_name":"<draft-policy>","sample_size":100}'
```

Every sampled pair is independently double-reviewed. Finalize and approve only
after the High precision target and minimum sample safeguards pass. At the
current checkpoint, 100/100 reviewed High pairs were Same, giving a Wilson 95%
lower bound of 96.30%, above the configured 95% target. This is high confidence,
not a guarantee that every future pair is correct.

The separate Positive Benchmark is optional diagnostic evidence for blocking
recall and is not a canary prerequisite:

```bash
bench --site <target-site> execute db_connector.api_fuzzy_evaluation.install_positive_benchmark_run \
  --kwargs '{"policy_name":"<draft-policy>","sample_size":100,"double_review_count":20}'
```

### 7.9 Promote the policy and generate Tiered Evidence

Only after both required evaluation runs are Completed and Approved against
the unchanged policy snapshot, promote the Draft policy in Desk with **Promote
to Pilot**, or use the bench-only deployment helper:

```bash
bench --site <target-site> execute db_connector.api_fuzzy_canary.install_promote_policy_to_pilot \
  --kwargs '{"policy_name":"<draft-policy>"}'
```

Start the recommendation canary from **CCD Matching Policy → Start
Recommendation Canary**, or:

```bash
bench --site <target-site> execute db_connector.api_fuzzy_canary.install_canary_run \
  --kwargs '{"policy_name":"<pilot-policy>"}'
```

The long-queue job freezes the CCD population, regenerates the complete
governed candidate set, evaluates Tiered Gated evidence, applies source
coverage/staleness/one-to-many/transitive/cluster-safety gates, and writes only:

- `Proposed` Tiered recommendations for safe High components;
- `Exception` recommendations and component-review work for blocked High
  components; and
- the random Tiered QC cohort.

It does not create Identity Decisions, Groups, Memberships, Exclusions, change
`CCD Master`, set `Is Matched?`, or write the legacy Matching Score table.
Before proceeding, the canary must be Ready and its record/candidate counts,
skipped/truncated blocks, policy hash, Proposed/Exception distribution, and QC
sample must be understood.

### 7.10 Generate the optional Splink Review Pool

Splink is optional human-review prioritization; it is not part of Tiered
activation. From a Ready/Active canary, use **Create Splink Review Queue**, or:

```bash
bench --site <target-site> execute db_connector.api_fuzzy_review_queue.install_review_queue \
  --kwargs '{"canary_name":"<canary-run>"}'
```

This job does not reuse a portable binary model. It verifies the approved
Threshold Evaluation and adapter, reproduces the frozen canary population and
candidate count, reconstructs the bounded fitting cohort, fits Splink, excludes
Tiered High and previously human-used pairs, scores every remaining governed
pair in 20,000-pair batches, and stores only pairs at or above the approved
Review cutoff. Any stale record, missing endpoint, changed candidate count,
truncated/skipped block, adapter mismatch, or missing/duplicate score fails the
whole run closed.

The resulting `CCD Match Review Candidate` rows are an optional ranked pool.
Creating the pool assigns no reviewer work and creates no identity links.
Review assignments should be bounded by capacity; Same still requires two
distinct reviewers and disagreement/Unsure requires adjudication.

### 7.11 Evidence-rebuild acceptance record

Record the following in the target migration evidence package:

- policy name/version and policy snapshot SHA-256;
- source-profile count and any skipped/unmapped CCD Registrations;
- Threshold Evaluation name, snapshot, labels, approval, Splink adapter,
  training-record count, calibration/held-out metrics, and Review cutoff;
- High Tier Validation name, sample result, Wilson interval, and approval;
- optional Positive Benchmark result;
- canary name, record/candidate counts, Proposed/Exception/component/QC counts,
  skipped/truncated-block result, and snapshot time;
- optional Splink Review Queue name, eligible/scored/stored counts and cutoff;
- proof that materialization remained off throughout; and
- the exact component and complete private-app revisions.

Never copy the current numeric checkpoint counts or cutoff into a new data
population as if they were configuration constants. The target must produce and
approve its own values whenever the evidence is rebuilt.

## 8. Mode C — this managed Docker layout

The checked-in `deployment/deploy_db_connector.sh` is suitable only when its
assumptions are true:

- host root defaults to `/root/erpnext_docker_volume`;
- site defaults to `frontend`;
- containers are named `frappe_docker-backend-1`,
  `frappe_docker-scheduler-1`, `frappe_docker-queue-long-1`, and
  `frappe_docker-queue-short-1`;
- raw public assets are copied into the app filesystem of
  `frappe_docker-frontend-1`;
- the complete private app already exists either in the live mount or
  `persistent_apps/db_connector`; and
- the private `hksr` app is available from the backend container.

`ERPNEXT_VOLUME_ROOT` and `FRAPPE_SITE` are configurable, but container names
and several internal paths are not. On a different Docker Compose project,
review and adapt the script rather than running it unchanged.

The frontend container has a filesystem separate from the backend container.
Its `sites/assets/db_connector` entry is a symlink to:

```text
/home/frappe/frappe-bench/apps/db_connector/db_connector/public
```

Do not copy the backend's `sites/assets/db_connector` symlink as though it were
the asset contents: that can leave a valid-looking symlink whose frontend-local
target contains only `.gitkeep`. The reviewed deployment script copies
`persistent_apps/db_connector/db_connector/public/.` directly into that target
inside `frappe_docker-frontend-1` and fails if
`js/ccd_master_identity_resolution.js` is still missing.

For the exact current layout, the normal full deployment is:

```bash
ERPNEXT_VOLUME_ROOT=/root/erpnext_docker_volume \
FRAPPE_SITE=frontend \
/root/erpnext_docker_volume/deploy_db_connector.sh
```

`--code-only` is appropriate only when no DocType, dependency, hook, or built-
bundle migration is required. It still synchronizes raw public files to the
frontend container. A new server or first identity-resolution installation
needs a full migrate/build deployment so `after_migrate` installs the managed
Client Script and schema.

## 9. Mandatory post-transfer validation

Do not enable live materialization until all checks pass.

### 9.1 Software and process checks

- target Frappe/ERPNext/app revisions match the approved manifest;
- every web/scheduler/queue process imports all identity API modules;
- `bench --site <target-site> doctor` reports healthy workers;
- dependency imports for DuckDB, Splink, RapidFuzz, pypinyin, and hanziconv pass;
- the app asset build and raw public renderer exist on the frontend;
- no migration/import errors appear in logs.

For this managed Docker layout, verify the exact frontend path and the served
HTTP response. Adapt the service name and Host header on another topology:

```bash
docker exec frappe_docker-frontend-1 test -f \
  /home/frappe/frappe-bench/apps/db_connector/db_connector/public/js/ccd_master_identity_resolution.js

docker exec frappe_docker-frontend-1 test -f \
  /home/frappe/frappe-bench/apps/db_connector/db_connector/public/js/ccd_match_component_review_list.js

docker exec frappe_docker-backend-1 curl --noproxy '*' --fail --silent \
  --show-error -H 'Host: <target-hostname>' \
  http://frontend:8080/assets/db_connector/js/ccd_master_identity_resolution.js \
  >/dev/null
```

Then prove that Frappe's actual form metadata contains the renderer. This is
the check that catches a custom DocType silently skipping `doctype_js`:

```bash
docker exec frappe_docker-backend-1 bash -lc \
  "bench --site <target-site> execute frappe.desk.form.meta.get_meta \
  --kwargs '{\"doctype\":\"CCD Master\",\"cached\":False}' \
  | grep -q load_identity_resolution"
```

Run the app tests in the target Bench environment:

```bash
cd /home/frappe/frappe-bench/apps/db_connector
PYTHONPATH=.:./db_connector \
  /home/frappe/frappe-bench/env/bin/python \
  -m unittest discover -s db_connector/tests -v
```

### 9.2 Schema and security checks

- every `CCD Identity *` and `CCD Match Review Batch*` DocType loads;
- CCD Master contains the Identity Resolution tab/custom HTML field;
- an enabled **CCD Master Identity Resolution** Client Script exists when CCD
  Master is custom, and its source contains `load_identity_resolution`;
- identity membership/exclusion/event indexes exist;
- `CCD Match Reviewer`, `CCD Match Sensitive Reviewer`, and System Manager
  permissions behave as designed;
- ordinary reviewers cannot retrieve CCD record IDs through the identity view;
- Sensitive Reviewers/System Managers see only their permitted values; and
- Identity Resolution Settings reports materialization disabled.

### 9.3 Data checks by mode

For a full-site transfer, compare aggregate counts with the frozen source
manifest. At the current checkpoint they are:

| Object | Expected current count |
| --- | ---: |
| Proposed Tiered recommendations | 3,522 |
| Approved Tiered recommendations | 6 |
| Exception recommendations | 433 |
| Exception component reviews | 191 |
| Splink Review Pool | 11,177 |
| Splink work assigned | 0 |
| Identity Decisions | 10 |
| Active Identity Groups | 10 |
| Active Identity Memberships | 23 |
| Active Identity Exclusions | 7 |
| Applied Activation batches | 2 |
| Finalized/Applied Component Reviews | 4 |
| Finalized Splink candidates | 2 (not materialized) |
| Human Review batches | 0 |

These are checkpoint values, not permanent constants. If migration occurs after
authorized operations, compare against a fresh signed source manifest instead.

For a feature-only installation, zero source recommendations are expected until
the target generates its own evaluation/canary.

### 9.4 Zero-write functional check

If a Ready canary exists, run **Preview Approve All** and confirm:

- `zero_write = true`;
- selected components are complete;
- unsafe/stale/conflict counts are understood;
- no Identity Decision/Group/Membership/Event count changes; and
- CCD Master, Matching Score, and `Is Matched?` remain unchanged.

Do not test Apply merely to prove the migration. Apply is an activation action
and requires its own backup, authorization, and bounded batch.

Also run the legacy fingerprint preview from section 6.5 and require all of the
following before activation:

- `totals.missing_rows = 0` after any approved repair;
- `stale_rows = 0` or every stale row has been moved to a new canary/queue;
- `corrupt_parent_rows = 0`;
- `inconsistent_existing_rows = 0`; and
- Materialization remains off.

### 9.5 CCD Master identity-view acceptance test

Test all three display cases after clearing the target cache and restarting the
web process:

1. open a CCD Master that has a current Identity Membership;
2. confirm **Identity Resolution** shows `Linked`, the Identity Group, decision
   origin/policy version, and every current member permitted for that role;
3. open a singleton from an applied Partial Match or All Different decision and
   confirm the tab shows `Resolved Separately`, the count of current Different
   relationships, and its decision link(s);
4. open a CCD Master with neither a current Membership nor a current Different
   resolution and confirm the tab shows `Not Grouped`;
5. confirm a Sensitive Reviewer/System Manager can follow permitted record
   links while an ordinary reviewer receives masked member aliases; and
6. in browser developer tools, confirm the
   `get_identity_resolution` request succeeds rather than leaving a blank tab.

`Resolved Separately` is dynamic, not a permanent ban. Each Different exclusion
is scoped to the two records and the governed identity fingerprints captured by
that decision's frozen policy. If either governed fingerprint changes, the old
exclusion no longer describes the current evidence. A later approved link to a
non-excluded record may create a Membership, and an active Membership takes
display precedence. A proposed link that directly contradicts a still-current
exclusion remains safety-blocked and must be reviewed/corrected; it is not
silently allowed.

If the tab exists but is completely empty, check in this order:

- FormMeta contains `load_identity_resolution` (managed Client Script for a
  custom CCD Master, or `doctype_js` for a standard CCD Master);
- the frontend-local public file exists and nginx serves it with HTTP 200;
- the browser has reloaded form metadata after `bench --site <site> clear-cache`;
- the API call returns `Linked`, `Resolved Separately`, or `Not Grouped` for the
  signed-in role; and
- backend/browser logs contain no JavaScript, permission, or API exception.

Do not re-enable Materialization or reapply an Applied batch to repair an empty
view. First compare the Membership table and API response: if they exist, the
problem is presentation/deployment, not materialization.

## 10. Rollback and recovery

### 10.1 Migration failure before cutover

- keep the source site authoritative and read-only maintenance on the target;
- correct target code/app/version issues and repeat restore; or
- discard the failed target site using the target platform's approved process.

No source rollback is needed because traffic never moved.

### 10.2 Failure after cutover but before new target writes

- return routing to the still-frozen source;
- preserve target logs for diagnosis; and
- do not merge target database changes back manually.

### 10.3 Failure after target writes

Stop and obtain governance/technical review. A blind DNS rollback would lose
new target decisions. Choose an authoritative database and restore/reconcile it
through an approved plan.

### 10.4 Identity decision correction

Normal identity correction is not a database restore:

- end the incorrect Membership with a reason;
- revalidate or supersede the Group/Decision;
- preserve append-only Events and human/model provenance; and
- create a new reviewed decision if needed.

Use a full backup restore only for disaster recovery or a formally approved
whole-site rollback.

## 11. Activation after migration

Successful migration does not authorize activation. The separate sequence is:

1. verify the migrated site with materialization off;
2. run Preview Approve All and understand every unsafe/stale conflict;
3. select complete components for a bounded pilot and any deliberate holdout;
4. create and inspect the frozen Activation Batch while materialization is off;
5. approve the frozen batch, which still creates no identity links;
6. take a fresh full backup immediately before the write window;
7. enable `materialization_enabled` as a System Manager;
8. apply it, which reruns all safety checks and creates reversible objects;
9. disable materialization immediately after Apply unless another authorized
   operation explicitly requires it; and
10. verify groups, memberships, events, masking, idempotency, and QC before any
    wider wave.

Enabling the global switch enables both approved Tiered batch application and
materialization of newly finalized human Same/Different/component decisions.
For a tightly controlled Tiered pilot, coordinate or pause human review
finalization during the short enabled window, then turn the switch off after
the batch is verified. Tiered recommendations themselves can materialize only
through an approved Activation Batch; human-final routes use the same safety
service but do not require a Tiered batch.

`automation_paused = 0` is the normal untripped state. A value of `1` means QC
or governance has paused new automatic Tiered materialization. The circuit
breaker remains active in either state and can set the value to `1` after a
confirmed QC Different, overdue QC, or a failing rolling precision condition.

### 11.1 What the frozen hashes and fingerprints mean

The system uses SHA-256 for several different purposes. The names describe the
meaning of the input, not a different cryptographic algorithm:

| Value | Input/meaning | Purpose |
| --- | --- | --- |
| Policy version | Human-readable label such as `pilot-1.6` | Reporting and governance reference |
| Policy snapshot JSON | Complete canonical policy/source configuration | Reproduce exactly what was evaluated |
| Policy snapshot SHA-256 | Hash of the canonical snapshot JSON | Detect any snapshot mutation/corruption |
| Pair fingerprint | Policy version plus an ordered pair of CCD record IDs | Stable pair correlation without displaying IDs |
| Component fingerprint | Sorted set of every CCD record ID in the connected component | Freeze the complete case boundary |
| Identity fingerprint | Normalized governed identity evidence, source, and policy version for one CCD record | Detect identity-relevant changes without reacting to administrative notes |
| Batch selection fingerprint | Canary, selection method, components, and recommendation names | Prove the approved batch is the selection that was reviewed |
| Idempotency key | Origin plus canonical final groups/exclusions | Prevent duplicate Decisions, Groups, Memberships, and Events on retry |

A fingerprint is an opaque deterministic digest, not a probability, match
score, encryption mechanism, or proof that two people are the same.

### 11.2 Activation Batch review state

Creating a batch runs its zero-write safety preview, freezes the exact
selection, and creates the document directly in `Reviewed` state. There is no
separate button that changes Draft to Reviewed in the current UI. This does not
remove the review responsibility: the System Manager must inspect counts,
policy/snapshot, component items, and zero unsafe/stale conflicts before using
**Approve Batch**. Approval is a separate recorded action and still creates no
Membership. Only **Apply Approved Batch** writes identity objects.

In **Complete Components**, each item shows its source pair(s) and a **Review
Pair(s)** button. The protected review dialog lists the frozen `R1`, `R2`, …
CCD records, links to each permitted CCD Master and Match Recommendation, and
the current pair evidence with an explicit stale warning if a CCD record changed
after the canary snapshot. The selected record pairs are frozen; raw evidence
values are deliberately not duplicated into the batch document. This is the
human-readable review surface; the component fingerprint remains the tamper-
evident stable key. Only a System Manager or Sensitive Reviewer can load this
detail, and the ordinary recommendation evidence masking rules remain in force.

### 11.3 Development-server flow rehearsal

A development rehearsal may use five complete two-record Tiered Proposed
components from any governed source when its purpose is to learn and demonstrate
the workflow rather than estimate production impact.

A formal holdout is optional for this rehearsal. The concepts are distinct:

- **pilot wave** — the 5–10 components that will be activated; and
- **deliberate holdout** — a separately identified comparable component set
  intentionally not activated so outcomes can later be compared.

Everything outside a five-component pilot remains unactivated backlog, but it
is not a controlled holdout unless it was explicitly selected and recorded for
comparison. For a first functional development test, use five pilot components
and no deliberate holdout.

On the Ready canary form, choose **Identity Rollout → Create Pilot Wave** and
enter `5` under **Complete components**. The current Desk dialog does not accept
CCD record IDs. With no explicit component keys supplied, the server selects
the first five available complete components in deterministic component-
fingerprint order and freezes them as Activation Batch items. If a specific
component must be excluded, open one of its Proposed recommendations and use
**Hold Complete Component for Later/Demo** before batch creation. The hold
applies to the entire component.

The component limit is counted *after* held components are excluded. Therefore:

- hold `0`, enter `5` → 5 pilot components and no deliberate holdout;
- hold `2`, enter `5` → 5 different pilot components plus 2 held components;
- hold `2`, enter `3` → 3 pilot components plus 2 held components, for a
  five-component pilot/holdout comparison set.

Materialization being enabled does not process the held components or the
unselected backlog. **Apply Approved Batch** processes only the exact frozen
items in that approved batch. A held component cannot be included in a newly
created batch, and holding a selected component after batch creation makes
Apply stop instead of silently changing the frozen scope.

Do not check **Synthetic demonstration batch only** for real development CCD
records. That flag marks the resulting audit events as demonstration events; it
does not turn Apply into a dry run. With Materialization enabled, Apply still
creates real active Identity Decisions, Groups, and Memberships on that site.
Use the flag only when every selected CCD record is deliberately synthetic/fake
and the resulting audit trail must be labelled as a demonstration. Preview is
the zero-write operation.

Administrator may perform the System Manager steps if its resolved roles
include the exact `System Manager` role. The existing `CCD Match Reviewer` and
`CCD Match Sensitive Reviewer` users can continue performing Tiered QC. A
single Administrator account cannot provide two independent Same confirmations:
two different user accounts must submit the ordinary QC labels, while a System
Manager handles adjudication when required.

If the development site has no active imports, canary/evaluation jobs, or human
finalization, a special maintenance window is unnecessary. During the few
minutes in which materialization is enabled, avoid:

- changing the selected CCD Master records;
- starting a new evaluation, canary, Splink queue, or bulk import; and
- finalizing unrelated human Same/Different/component decisions, because the
  materialization switch is global.

A verified nightly off-host backup is acceptable for a low-risk development
rehearsal if the team accepts losing changes made since that backup. A fresh
pre-Apply backup is still preferred because it provides an exact rollback
point. Remote backup storage satisfies the independence requirement; verify
that the backup completed, is non-empty, and has a practiced restore procedure.

It is safe to create, inspect, and approve the frozen batch before taking that
fresh checkpoint: those steps write only the governance/batch document and do
not create live identity links. Once the exact batch is approved and the team
is ready for the short write window, take the backup so it includes the latest
site state and is as close as practical to Apply. Then enable Materialization,
apply that one batch, turn Materialization off, and verify. If rollback is
needed, that checkpoint returns the site to the state immediately before the
identity Groups and Memberships were created.

### 11.4 Bulk materialization of finalized exception components

The component bulk action is for human-finalized **CCD Match Component Review**
documents, not for every Recommendation whose status is Exception. Use it after
the required reviewers have produced an `Agreed` or `Adjudicated` component
partition and its **Identity Materialization** status is `Pending` or
`Exception`:

1. open the **CCD Match Component Review** list;
2. filter **Identity Materialization** to `Pending` or `Exception`;
3. check exactly the number of complete components wanted for this operation;
4. choose **Actions → Preview Selected Identity Materialization**;
5. inspect each decision and the planned group, membership, exclusion, and
   safety counts; and
6. with Materialization enabled, choose **Materialize Selected (N)** and confirm.

The checked-row count is the operation size: selecting 1, 3, or 10 rows processes
exactly 1, 3, or 10 complete components. The server limit is 25 per operation so
the review and verification remain bounded. Preview is always zero-write. Apply
reruns preflight and treats the selected set atomically; if any component no
longer passes, none of the selected components are committed. Already-Applied,
unfinalized, and stale rows are rejected. The action requires the exact `System
Manager` role, including when the account is Administrator.

For a short controlled window, first finalize components while Materialization
is off so they remain `Pending`; take the required backup, enable
Materialization, preview the explicit selection again, apply it, disable the
switch, and verify the resulting Identity views and audit objects. This bulk
action replaces repetitive one-by-one **Retry Identity Materialization** clicks;
the individual retry remains available for diagnosis.

Both the individual and bulk component routes now require a complete frozen
identity fingerprint and frozen `modified` value for every participant. Splink
human decisions enforce the same rule. The materializer acquires CCD record
locks and then compares current values with both frozen values immediately
before writing; missing snapshot metadata fails closed as
`frozen_identity_snapshot_incomplete`.

## 12. Sign-off checklist

Before declaring transfer complete, record:

- [ ] chosen transfer mode and target topology;
- [ ] source/target version and installed-app manifests;
- [ ] complete private app sources and reviewed component commit;
- [ ] verified database/files/config backup and checksums;
- [ ] securely restored encryption key;
- [ ] successful restore/migrate/build/restart;
- [ ] healthy web, scheduler, and queue imports;
- [ ] test-suite result;
- [ ] schema/index/custom-field result;
- [ ] aggregate source-versus-target count comparison;
- [ ] permissions/masking checks;
- [ ] materialization disabled;
- [ ] zero-write preview result, when a canary exists;
- [ ] rollback owner/window; and
- [ ] separate activation authorization status.

## 13. Source-of-truth files

| Purpose | File |
| --- | --- |
| Current verified deployment boundary | `IDENTITY_RESOLUTION_IMPLEMENTATION_STATUS.md` |
| Workflow/data model/safety rules | `IDENTITY_RESOLUTION_WORKFLOW_PLAN.md` |
| Tiered/Splink algorithms, training, calibration, queue, and safeguards | `CCD_TIERED_EVIDENCE_AND_SPLINK_TECHNICAL_GUIDE.md` |
| Other-server transfer | `ERPNext_SERVER_MIGRATION_RUNBOOK.md` |
| Current-host Docker deployment | `deployment/deploy_db_connector.sh` |
| Idempotent schema/custom-field/Client-Script setup | `identity_resolution_setup.py` |
| Timestamp-guarded legacy snapshot repair | `identity_snapshot_backfill.py` |
| Exact versioned component | Git commit recorded in Document control |

If these sources disagree, stop before activation and reconcile the
documentation and deployed commit first.
