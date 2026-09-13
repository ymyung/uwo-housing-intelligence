# Data Pipeline

## Purpose

The pipeline converts semi-structured housing advertisements into reviewed, normalized, and auditable records. It deliberately does not publish directly to the frontend: an approved run must pass validation before the separate database import stage can change application data.

## Stages

| Stage | Implementation | Output and gate |
| --- | --- | --- |
| 0 — discovery | `scraper/collect_listing_urls.py` | Stable listing URLs in a versioned run directory. |
| 1 — extraction | `pipeline/uwo_listing_enricher.py` | Structured page fields plus deterministic description-derived values, original text, and provenance. |
| 2 — optional AI | `pipeline/ai_enricher.py` | Only unresolved fields are requested from local Ollama; evidence checks and contradictions create review flags. This stage can be explicitly skipped. |
| Manual review | `pipeline/review_workflow.py`, `pipeline/review_ui.py` | Reviewed corrections remain explicit and reproducible. |
| 3 — geocoding | `pipeline/geocoder.py` | Cached coordinates, provider evidence, confidence, failures, and review candidates. |
| 3 QC — canonicalization | `scripts/apply_geocode_qc.py`, `pipeline/canonical_rebuild.py` | Canonical CSV and quality disposition for import. |
| Approval | `pipeline/run_approval.py`, `pipeline/run_context.py` | Eligibility checks and artifact fingerprints prevent accidental or stale import. |
| 4 — persistence | `pipeline/database_importer.py` | Validated dry-run plan followed by a transactional, idempotent PostgreSQL import. |

`scripts/run_full_pipeline.ps1` orchestrates Stages 0-3 only. Stage 4 is intentionally separate.

## Evidence Priority

For every field, the merge order is:

1. explicit structured website data;
2. deterministic rules applied to captured title, description, and amenity tokens;
3. AI output only for a field that is still missing or ambiguous.

Existing structured and rule-promoted values are protected from AI overwrite. AI evidence must occur in the captured input, and contradictions or low-confidence hard fields are routed to review. Raw source values, normalized values, source labels, AI outputs, evidence, and review flags are retained where the schema supports them.

## Price Normalization

The pipeline preserves `price_text`, the numeric amount, and its period. A comparable monthly value is produced only when the period is supported:

- monthly and per-bedroom monthly values remain unchanged;
- weekly rent is multiplied by 52/12;
- daily rent is multiplied by 365/12;
- unknown or unsupported periods remain unknown.

This avoids silently treating every advertised number as monthly rent.

## Availability and Sublets

Availability and rental arrangement are separate concepts.

- A closed May-August range is classified as summer availability.
- Summer availability alone does not imply a sublet.
- `is_sublet=true` requires explicit sublet, sublease, lease-transfer, or equivalent takeover evidence.
- Explicit negative language and conflicting evidence are handled conservatively.
- Structured availability dates outrank conflicting date ranges found in free text; the conflict remains reviewable.

## Geocoding and Location Safety

Addresses are normalized for lookup and cache reuse, while source address text is preserved. Geocoding records provider metadata and quality signals. Repeated addresses use the cache unless refresh policy requires another lookup.

The downstream location-visibility contract separates:

- whether a location is available to the product;
- whether a marker may be shown;
- whether route actions may be offered.

A listing can remain discoverable when its coordinates are withheld. Missing or questionable coordinates do not become zero-distance results.

## Validation and Approval

Run manifests record stage status, row counts, warnings, failures, and artifact fingerprints. Approval checks include expected stage progression and the current canonical file identity. Review queues cover AI ambiguity, manual corrections, geocode issues, and import concerns.

The database importer validates required fields and types before writing, generates a change summary/dry-run plan, and uses normalized listing and property identities to make repeated imports safe.

## Persistence and Change Detection

The schema separates:

- pipeline runs and approvals;
- geocode results;
- canonical properties;
- stable source listings;
- immutable listing observations;
- review items;
- versioned ranking and accessibility outputs.

A stable source URL/listing ID anchors listing identity. Normalized address evidence supports conservative property matching, with fuzzy or ambiguous cases reviewed rather than merged automatically. Semantic field comparisons identify updates; last-seen state supports possible removal and reactivation without deleting observation history. Pipeline corrections are distinguishable from confirmed source changes.

## External-Service Boundary

Collection, AI, hosted geocoding, database writes, municipal-data refresh, and routing workers are explicit operator actions. The synthetic sample in `data/samples/` exercises the application schema without invoking any of them.
