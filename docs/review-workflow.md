# Automated review workflow

The review workflow reduces deterministic Stage 2/3 review work without claiming
human judgment or calling AI/geocoding services. It operates on one explicitly
selected run and never approves or imports it.

For the visual human queue, map controls, audited draft saving, and remote
decision synchronization, see [Local human-review dashboard](review-ui.md).

## Commands

Analyze an authoritative local run or a copied review bundle:

```powershell
python -m pipeline.operator review-auto --run-id <run_id>
python -m pipeline.operator review-status --run-id <run_id>
```

Use `--run-dir <path>` to select a particular local copy. To execute against the
authoritative Windows SSH run and refresh the local bundle:

```powershell
python -m pipeline.operator review-auto --remote-host uwo-server --run-id <run_id>
python -m pipeline.operator review-status --remote-host uwo-server --run-id <run_id>
```

`review-auto` only writes review proposals and audit outputs. Apply current safe
decisions separately:

```powershell
python -m pipeline.operator apply-review-decisions --run-id <run_id>
```

For a remote authoritative run, add `--remote-host uwo-server`. Application
validates every listing fingerprint, skips stale records, atomically updates the
reviewed/geocoded inputs, rebuilds canonical and geocode-review artifacts, and
regenerates review and operator summaries.

## Source and decision policy

Evidence is considered in this order:

1. Structured website values
2. Deterministic parser/rule values
3. Explicit, unambiguous listing text
4. Current manual corrections tied to the listing evidence
5. AI or Codex-assisted proposals

Lower-priority evidence cannot silently replace higher-priority evidence.
Conflicts remain `human_review_required`. AI/Codex proposals are always human
required unless an independent deterministic rule establishes the same value.
Stored proposal summaries contain supporting/conflicting text and a concise
reasoning summary, never hidden chain-of-thought.

Every issue receives exactly one of:

```text
auto_resolved
human_review_required
accepted_as_unknown
excluded
already_resolved
```

The automatic workflow never emits `excluded`. Existing manual values are not
marked human-reviewed by automation. Resolved automated flags update
`needs_manual_review` without setting `manual_reviewed=true`.

## Deterministic rules

- Known month/week/day periods use the same Stage 4 conversion (`52/12` and
  `365/12`). A missing monthly value that is deterministically convertible is
  classified as `parser_failure`. Original price text, amount, and period remain
  stored.
- A missing period such as `$900` is documented as `period_ambiguous` and remains
  null rather than being assumed monthly. Invalid amounts remain human-required.
  A genuinely absent amount or explicit unsupported period is also documented as
  `genuinely_missing` or `non_monthly_convertible` and accepted as unknown.
  Conflicting period phrases are classified `human_review_required`.
- `May-Aug` is only summer availability. It never implies sublet.
- Sublet, gender, furnished, utilities, and bathroom values require explicit
  unambiguous phrases or a higher-priority structured/rule value.
- Cached geocodes are accepted only when the normalized listing address equals
  the query, exactly one Geoapify cache row matches current coordinates/status,
  confidence meets the configured threshold, the result is a full building
  match, and coordinates fall inside configured London-area bounds.
- Missing addresses, low-confidence/street/city matches, conflicting cache rows,
  and out-of-area coordinates remain human-required.

`ReviewConfig` contains the geocoding confidence and area bounds. Configuration
is recorded in the summary and covered by tests.

## Outputs and audit

All files are written atomically under:

```text
review/
  review-decisions.jsonl
  remaining-human-review.csv
  auto-resolved.csv
  accepted-unknown.csv
  review-auto-summary.json
```

The JSONL audit records the required identity, original/selected values, status,
stable reason, inspectable evidence, confidence, reviewer type, UTC timestamp,
and exact listing-input fingerprint. Historical decisions remain in the JSONL;
current outputs select records matching current fingerprints.

The remaining-human CSV groups overlapping issues by listing and includes source
URL, categories, fields, current/proposed values, reasons, evidence, description,
address, and coordinates. Accepted unknowns remain visible in their own output
and in the readiness summary.

For an explicitly human-approved proposal, edit the current JSONL record to set
`human_approved=true`, provide `human_reviewer`, set the reviewed
`selected_value`, and optionally record `human_approved_at_utc`. Application
rejects stale fingerprints or a missing reviewer. Source identities cannot be
modified. Automatic exclusion is unsupported.

## Application and approval safety

Applying decisions:

- accepts only current automatic/unknown/already-resolved records or explicitly
  human-approved records;
- preserves listing IDs and source URLs;
- rolls all affected artifacts back if rebuilding or summary generation fails;
- records applied decision IDs and fingerprints in manifest audit history;
- preserves resolved warnings in review history while removing only stale
  Stage 2/manual-review warnings;
- invalidates and audits any prior approval;
- never approves, migrates, connects to PostgreSQL, or imports.

Repeated application is idempotent. If evidence changes, the listing fingerprint
changes and the old decision is refused.

`ready_for_approval` requires no active fatal errors, no current metric
discrepancies, no human-required decisions, no unapplied safe decisions, current
decision/canonical fingerprints, documented accepted unknowns, and no canonical
import-eligibility blockers. It does not mean approved.

Once ready, print—but do not execute—the staging sequence:

```powershell
python -m pipeline.operator review-next-steps --run-id <run_id>
```

The printed sequence covers approval status, explicit approval, PostgreSQL
integration tests, an import and SQL verification against
`TEST_DATABASE_URL`, and backend verification. It never references
`DATABASE_URL`; production import remains a separate human-controlled workflow.
