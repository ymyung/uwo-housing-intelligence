# Versioned Stage 0-to-3 runs

Pipeline artifacts are isolated under `data/runs/<run_id>/`. Each run contains
Stage 0 discovery, Stage 1 extraction, Stage 2 enrichment/review, and Stage 3
geocoding/QC outputs plus `manifest.json`. The final Stage 3 files are
`stage3/canonical.csv` and `stage3/geocode_review.csv`. A completed run is not
automatically approved for database import; inspect its warnings and review
queues first.

Use the [automated review workflow](review-workflow.md) to resolve only
fingerprinted deterministic cases and produce a grouped unresolved queue before
performing the explicit approval steps below.

## Local development

For local Codex development, use the repository virtual environment and run
only deterministic tests unless live work is explicitly intended:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m compileall pipeline scraper scripts
```

An existing run is continued explicitly with `--resume` (or `-RunDir` in the
orchestrator). Individual stage CLIs also accept `--overwrite` for an intentional
rerun. Neither option is inferred merely because files already exist.

Geocoding uses the shared cache at `data/processed/geocode_cache.csv` by default,
outside individual run directories. To guarantee that Stage 3 performs no
network calls, use:

```powershell
python pipeline\geocoder.py --run-dir data\runs\<run_id> --resume --cache-only
```

Cache misses are retained in the output as `cache_miss`. Failed cached results
can be retried with `--refresh-errors`; cached results below the configured
threshold can be retried with `--refresh-low-confidence`.

For a later five-listing local smoke run (this performs live discovery and may
call configured Ollama and Geoapify services):

```powershell
.\scripts\run_full_pipeline.ps1 -SmokeTest -SkipLaterStages
```

For a cache-only smoke run, add `-CacheOnlyGeocoding`. To bypass AI or known
manual corrections explicitly, add `-SkipAI` or `-SkipManualFixes`.

## Full SSH execution

On the SSH execution machine, after entering the repository and activating its
environment, run the same repository-relative orchestrator for a full run:

```powershell
pwsh -File .\scripts\run_full_pipeline.ps1 -SkipLaterStages
```

Resume a particular run with:

```powershell
.\scripts\run_full_pipeline.ps1 -RunDir data\runs\<run_id> -SkipLaterStages
```

Inspect its manifest without running a pipeline stage:

```powershell
python -m pipeline.run_context summary --run-dir data\runs\<run_id>
```

Stage 4 database persistence is intentionally a separate, explicitly selected
boundary. See [Stage 4 PostgreSQL persistence](stage4-database.md) for schema,
dry-run, migration, lifecycle, and import instructions.

## Explicit run approval

Pipeline completion and import approval are separate decisions. Finalization
always leaves `canonical_for_import=false`; it never approves a run merely
because all stages reached terminal states. A reviewer must inspect the review
queues and run an explicit approval command against the authoritative run
directory.

First display the read-only status and review summary:

```powershell
python -m pipeline.run_context approval-status `
  --run-dir "data\runs\<run_id>"
```

Review the following artifacts before approval. The manifest is authoritative
for stage statuses, row counts, warnings, errors, configuration, and output
paths; the CSV queues contain the rows needing human judgment:

```text
manifest.json
stage2/review_queue.csv
stage2/reviewed.csv
stage3/geocode_review.csv
stage3/canonical.csv
```

The summary reports available aggregate counts for discovery, canonical rows,
Stage 1 failures, AI/manual review, AI errors, missing addresses, geocoding
failures and confidence, map readiness, suspicious or missing monthly prices,
sublet review, and manifest warnings. A metric is shown as `unavailable` when
the manifest or canonical schema does not contain enough information. No
descriptions, addresses, landlord details, credentials, or row-level values are
printed.

Preview a clean run without changing the manifest. `--note` is optional but is
recommended when it adds useful audit context:

```powershell
python -m pipeline.run_context approve `
  --run-dir "data\runs\<run_id>" `
  --approved-by "reviewer-name" `
  --note "Reviewed AI and geocode queues"
```

After reviewing the summary and blocking conditions, repeat with the explicit
confirmation:

```powershell
python -m pipeline.run_context approve `
  --run-dir "data\runs\<run_id>" `
  --approved-by "reviewer-name" `
  --note "Reviewed AI and geocode queues" `
  --confirm
```

When `material_warning_conditions` is not empty, approval is refused unless the
reviewer explicitly acknowledges the displayed conditions:

```powershell
python -m pipeline.run_context approve `
  --run-dir "data\runs\<run_id>" `
  --approved-by "reviewer-name" `
  --note "Reviewed unresolved Stage 2 and geocoding rows" `
  --acknowledge-warnings `
  --confirm
```

Acknowledgement is required for unresolved AI/manual-review rows, Stage 1
failures, AI errors, geocoding review/failure/low-confidence rows, missing
addresses or coordinates, suspicious or unresolved monthly prices, sublet
review flags, valid discovery/canonical count differences, and nonfatal
manifest warnings. The exact acknowledged condition list is stored in the
approval and its history event. A clean run records
`warnings_acknowledged=false` and needs no acknowledgement flag.

Approval requires a completed or completed-with-warnings run, valid terminal
Stage 0-to-Stage 3 records, supported Stage 2/manual skips, consistent row
counts, valid and unique Western source IDs, a nonempty discovery result, and a
present canonical CSV. It blocks failed/running runs, missing or failed stages,
Stage 3 skips (there is currently no supported Stage 3-skip policy), smoke or
limited runs, maximum-page/incomplete discovery signals, duplicate/missing
identities, and stale files. Nonfatal AI, geocode, or repeated-page warnings are
shown for human judgment and require explicit acknowledgement. Fatal manifest
errors, inconsistent counts, failed/nonterminal stages, missing outputs, and
incomplete discovery remain unapprovable even with acknowledgement.

The command records exact SHA-256 bytes for `stage3/canonical.csv` and a
deterministic JSON fingerprint of approval-relevant manifest content. The
manifest fingerprint excludes only `approval`, `canonical_for_import`, and
`updated_at_utc`, so adding approval metadata does not invalidate itself while
pipeline configuration, stage status/count, warnings, and provenance changes
do. A lock file serializes cooperating manifest writers, the existing
temporary-file/fsync/atomic-replace writer remains in use, and the reviewed
manifest and canonical snapshots are checked again immediately before writing.
Approval metadata includes `approval_version=1`, UTC timestamp, reviewer,
optional note, warning acknowledgement and condition list, both fingerprints,
and append-only history. Status displays stored/current fingerprints, version,
acknowledgement, history count, and explicit invalidity reasons without row data.

If legitimate run content changes, status reports the invalid fingerprint and
normal Stage 4 import rejects it. Review the changed run and use the same
confirmed approval command again. Reapproval appends an audit event with the
previous status and fingerprints rather than overwriting history.

### Verify a committed approval fixture after checkout

Committed CSV and JSON files under `tests/fixtures/` are checked out with LF
line endings on every platform. Confirm the policy for the canonical fixture:

```powershell
git check-attr text eol -- tests/fixtures/runs/postgres_valid/stage3/canonical.csv
```

The result must report `text: set` and `eol: lf`. The following focused check
prints the working-tree hash, the exact `HEAD` blob hash, the manifest's stored
hash, and equality results without printing fixture contents:

```powershell
python -c "import hashlib,json,subprocess; from pathlib import Path; p='tests/fixtures/runs/postgres_valid/stage3/canonical.csv'; m='tests/fixtures/runs/postgres_valid/manifest.json'; w=Path(p).read_bytes(); h=subprocess.check_output(['git','show','HEAD:'+p]); s=json.loads(Path(m).read_text(encoding='utf-8'))['approval']['canonical_csv_fingerprint']; print('working='+hashlib.sha256(w).hexdigest()); print('head='+hashlib.sha256(h).hexdigest()); print('manifest='+s); print('working_matches='+str(hashlib.sha256(w).hexdigest()==s).lower()); print('head_matches='+str(hashlib.sha256(h).hexdigest()==s).lower())"
```

Both match results must be `true`. Do not repair a mismatch by editing a
fingerprint or using the importer override. On an existing checkout where the
fixture path has no local changes, refresh it from `HEAD` so the new attributes
are applied, then rerun the checks:

```powershell
git status --short -- tests/fixtures
git restore --source=HEAD --worktree -- tests/fixtures
```

Revoke approval without changing any CSV or deleting the run:

```powershell
python -m pipeline.run_context unapprove `
  --run-dir "data\runs\<run_id>" `
  --changed-by "reviewer-name" `
  --note "Additional geocode review required"
```

Unapproval sets `canonical_for_import=false`, retains the last approval fields,
and appends an unapproval event. The older `--unapproved-by` and `--reason`
spellings remain supported. Repeating unapproval on an already-unapproved run
is rejected clearly. Older manifests without `approval` are safely reported as
unapproved and can go through the same evaluation and confirmation flow.

Run approval where the authoritative run directory lives. Local synthetic and
smoke artifacts stay local. For a full run produced on an execution machine,
pull reviewed code there and run the same status/approval commands against that
run directory; do not hand-edit or independently fork its manifest.
