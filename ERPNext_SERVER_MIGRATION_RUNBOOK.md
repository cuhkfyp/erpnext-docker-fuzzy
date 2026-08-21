# ERPNext Server Migration and Transfer Runbook

## Document control

| Item | Value |
| --- | --- |
| Purpose | Move the guarded CCD matching and identity-resolution setup to another ERPNext server |
| Runbook date | 2026-08-21 UTC |
| Implementation code checkpoint | Git commit `a0ae535` or a reviewed successor |
| Source site at writing | `frontend` |
| Source framework baseline | Frappe 15.73.0 / ERPNext 15.70.0 |
| Required activation state during transfer | Live materialization disabled |
| Includes | Feature-only installation, full-site transfer, validation, cutover, rollback, and new-centre guidance |
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
bench --site <target-site> build --app db_connector
bench --site <target-site> clear-cache
```

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
- CCD Master `doctype_js` for `public/js/ccd_master_identity_resolution.js`;
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

## 8. Mode C — this managed Docker layout

The checked-in `deployment/deploy_db_connector.sh` is suitable only when its
assumptions are true:

- host root defaults to `/root/erpnext_docker_volume`;
- site defaults to `frontend`;
- containers are named `frappe_docker-backend-1`,
  `frappe_docker-scheduler-1`, `frappe_docker-queue-long-1`, and
  `frappe_docker-queue-short-1`;
- frontend assets are copied to `frappe_docker-frontend-1`;
- the complete private app already exists either in the live mount or
  `persistent_apps/db_connector`; and
- the private `hksr` app is available from the backend container.

`ERPNEXT_VOLUME_ROOT` and `FRAPPE_SITE` are configurable, but container names
and several internal paths are not. On a different Docker Compose project,
review and adapt the script rather than running it unchanged.

For the exact current layout, the normal full deployment is:

```bash
ERPNEXT_VOLUME_ROOT=/root/erpnext_docker_volume \
FRAPPE_SITE=frontend \
/root/erpnext_docker_volume/deploy_db_connector.sh
```

`--code-only` is appropriate only when no DocType, dependency, hook, or asset
migration is required. A new server or this identity-resolution feature needs a
full migrate/build deployment.

## 9. Mandatory post-transfer validation

Do not enable live materialization until all checks pass.

### 9.1 Software and process checks

- target Frappe/ERPNext/app revisions match the approved manifest;
- every web/scheduler/queue process imports all identity API modules;
- `bench --site <target-site> doctor` reports healthy workers;
- dependency imports for DuckDB, Splink, RapidFuzz, pypinyin, and hanziconv pass;
- the app asset build exists on the frontend; and
- no migration/import errors appear in logs.

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
| Proposed Tiered recommendations | 3,528 |
| Exception recommendations | 433 |
| Exception component reviews | 191 |
| Splink Review Pool | 11,177 |
| Splink work assigned | 0 |
| Identity Decisions/Groups/Memberships | 0 |
| Activation/Review batches | 0 |

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
2. take a fresh full backup;
3. select complete components for a bounded pilot and any deliberate holdout;
4. enable `materialization_enabled` as a System Manager;
5. create and inspect the frozen Activation Batch;
6. approve it;
7. apply it, which reruns all safety checks and creates reversible objects;
8. verify groups, memberships, events, masking, idempotency, and QC; and
9. disable materialization or stop further waves until the pilot is accepted.

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
| Other-server transfer | `ERPNext_SERVER_MIGRATION_RUNBOOK.md` |
| Current-host Docker deployment | `deployment/deploy_db_connector.sh` |
| Idempotent schema/custom-field setup | `identity_resolution_setup.py` |
| Exact versioned component | Git commit recorded in Document control |

If these sources disagree, stop before activation and reconcile the
documentation and deployed commit first.
