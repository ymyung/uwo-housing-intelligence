# Remote pipeline operator

The operator runs or resumes Stage 0 through Stage 3 on a Windows SSH host, copies a small review bundle locally, and stops. It never approves data or imports it into PostgreSQL/Supabase.

## Configuration

Copy `config/operator.example.toml` to the ignored `config/operator.toml`. Set an SSH alias, the remote checkout path, and the Python virtual-environment path. The SSH alias should be configured in the local OpenSSH config and should authenticate without putting a password in this repository.

Configuration precedence is CLI options, then `config/operator.toml` (or `--config`), then safe defaults. The remote host has no usable default and is required. The remote shell may be `cmd.exe`; the operator explicitly launches Windows PowerShell 5.1 using `powershell.exe` and encoded commands. PowerShell 7 is not required.

The remote `.env` remains the source of `GEOAPIFY_API_KEY`. The operator reports only `configured`, `missing`, or `unreadable`; it neither prints nor copies the value.

## Commands

From the repository root in Windows PowerShell:

```powershell
python -m pipeline.operator preflight --remote-host ssh-alias
python -m pipeline.operator run --remote-host ssh-alias
python -m pipeline.operator status --remote-host ssh-alias --run-id <run_id>
python -m pipeline.operator resume --remote-host ssh-alias --run-id <run_id>
```

`resume` always requires an explicit run ID. No “latest” run is selected. Use `--retry-ai-errors` to deliberately rerun a Stage 2 result that completed with row-level AI errors. Use `--refresh-artifacts` to replace an existing local review bundle. A full run refuses tracked local changes by default; `--allow-dirty-local` is a recorded development-only override.

For a completed legacy run whose only problem is stale manifest errors, reconcile
bookkeeping without executing Stage 0–3:

```powershell
python -m pipeline.operator resume --remote-host ssh-alias --run-id <run_id> --reconcile-only
```

Then refresh its local review bundle, still without rerunning completed stages:

```powershell
python -m pipeline.operator resume --remote-host ssh-alias --run-id <run_id> --refresh-artifacts
```

When Stage 2 reviewed rows are newer than Stage 3's copied enrichment columns,
rebuild only the canonical and geocode-review artifacts from the current
`stage2/reviewed.csv` and `stage3/geocoded.csv`:

```powershell
python -m pipeline.operator rebuild-canonical --remote-host ssh-alias --run-id <run_id>
```

This operation joins rows by `listing_id`, validates exact ID-set and source-URL
agreement, reruns only offline geocode quality rules, records artifact hashes and
the prior discrepancy in `canonical_rebuild_history`, writes
`review-index.json`, and refreshes the local review bundle. It does not run
discovery, extraction, AI, geocoding, approval, or database import. Ollama and
Geoapify checks are therefore skipped.

Deterministic review, safe decision application, and readiness commands are
documented in [Automated review workflow](review-workflow.md). These commands
also skip Ollama and Geoapify service checks and never approve or import.

The [local human-review dashboard](review-ui.md) groups remaining issues and
stores fingerprinted human drafts. `review-ui --remote-host` browses the already
copied bundle without SSH; `review-sync-decisions` is the separate explicit,
validated remote merge step.

Bookkeeping-only and status operations validate SSH, Git, Python, repository files,
and the manifest but do not require Ollama or Geoapify. Service checks run before
any processing stage is executed.

Add `--json` for one machine-readable result or `--verbose` for diagnostic operation. Configuration path overrides are available as `--config`, `--remote-project-path`, `--remote-python-path`, and `--review-root`.

## Preflight and recovery

Local preflight verifies the repository, Git branch and commit, tracked working-tree state, `git`, `ssh`, `scp`, Python, the host value, and the review directory. Remote preflight verifies repository files, the configured Python and imports, disk space, run-directory writes, the exact Git commit, `.env` loading, Geoapify configuration, the Ollama HTTP API, and the configured model. It makes no Geoapify request.

The operator combines manifest stage status, row-count presence, and artifact existence. It skips successful expensive stages. A Stage 2 resume starts at Stage 2 and then reruns manual fixes, Stage 3, and QC. A Stage 3 resume starts at Stage 3. Missing QC starts at QC only. Systemic AI failures fail Stage 2, keep Stage 0 and Stage 1, and stop before geocoding. Only transient SSH and SCP failures receive bounded, increasing-delay retries.

Successful Stage 3 QC produces this local bundle:

```text
data/remote-runs/<run_id>/
  manifest.json
  review-index.json
  stage2/review_queue.csv
  stage2/reviewed.csv
  stage3/geocode_review.csv
  stage3/canonical.csv
  operator-summary.json
```

File sizes and SHA-256 hashes are checked after copying. Caches and remote logs are not copied. Existing bundles are never overwritten without `--refresh-artifacts`.

Summary counts use the current copied manifest and stage-specific review artifacts.
When downstream CSVs retain stale upstream columns, the report uses the current
stage artifact and records the disagreement in `metric_discrepancies`.

## Manual safety gates

Review the copied AI and geocoding queues and canonical CSV. Approval remains an explicit, separate command documented in `docs/pipeline-runs.md`; database import remains the separate Stage 4 workflow in `docs/stage4-database.md`. The operator does not invoke either command and never uses a noncanonical override.

## Exit codes

| Code | Meaning |
| ---: | --- |
| 0 | Preflight passed or run is ready for approval |
| 2 | Local preflight/configuration failure |
| 3 | Remote preflight failure |
| 4 | Local/remote Git commit mismatch |
| 5 | Remote execution or verified copy failure |
| 6 | Invalid run or unsafe local artifact overwrite |
| 7 | Run completed but human review is required |

Operator logs are sanitized and contain no intended secret values. Automatic service startup, package installation, Git synchronization, Docker, approval, and database import are deliberately out of scope.
