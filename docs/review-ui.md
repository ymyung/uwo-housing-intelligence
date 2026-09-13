# Local human-review dashboard

The review dashboard is a loopback-only FastAPI application for inspecting and
drafting human decisions against one fingerprinted Stage 3 run bundle. It does
not run scraping, AI, geocoding, approval, migration, or database import.

The page is deliberately separate from the production React/Supabase frontend.
It uses small static HTML, CSS, and JavaScript assets and Leaflet, matching the
project's existing map library without requiring Node.js to launch a review.
Leaflet and OpenStreetMap tiles are loaded during interactive use; if they are
unavailable, all coordinates, evidence, and decision controls remain usable.

## Requirements and launch

Install the existing Python requirements and launch from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

.\.venv\Scripts\python.exe -m pipeline.operator review-ui `
  --run-id 20260801T045513214889Z_6e17c3f `
  --reviewer "Hansen"
```

The default URL is `http://127.0.0.1:8765/` and the browser opens automatically.
Use `--no-open`, `--port <port>`, or `--read-only` as needed. A reviewer can also
be entered in the page, but a nonblank name is required before a decision saves.
Reviewer names are audit labels, not secrets.

Local runs are selected from `data/runs/<run_id>` first and copied remote bundles
from `data/remote-runs/<run_id>` second. Use `--review-root <directory>` for an
explicit configured copy root. Run IDs are validated and cannot contain path
separators or traversal components.

For a copied remote bundle, `--remote-host` selects local-copy mode and does not
open SSH:

```powershell
.\.venv\Scripts\python.exe -m pipeline.operator review-ui `
  --remote-host uwo-server `
  --run-id 20260801T045513214889Z_6e17c3f `
  --reviewer "Hansen"
```

## Queue and evidence

All current human-required issues for one listing are grouped into one page.
The sidebar supports previous/next and keyboard arrow navigation, category,
field, reason, confidence, map-readiness, missing-price, decision-state, and
listing/address/URL search. Progress reports completed listings and individual
issues separately because categories overlap.

Each page shows:

- listing identity, current fingerprint, source URL, title, and categories;
- original description and structured website fields;
- deterministic `*_rule` fields, current reviewed fields, manual provenance,
  AI values, stored evidence summaries, and conflicts;
- original price fields, classification, conversion reason, and conflict text;
- availability dates, lease data, summer classification, and sublet evidence;
- address, normalized query, provider, coordinates, confidence, precision,
  cache-evidence status, London bounds, map readiness, and campus distance;
- an append-preserving audit timeline of automated decisions, human drafts,
  application events, and canonical rebuilds.

The page always displays the project rule that May–Aug means summer availability
and does not by itself mean sublet. Hidden model reasoning is never loaded or
shown; only stored evidence and concise summaries are presented.

## Map and geocode review

The Leaflet map displays the current listing marker, Western University reference
point, and configured London-area rectangle. Reviewers can zoom, copy coordinates,
open Google Maps or OpenStreetMap, and click/drag a temporary manual marker.
Google Maps is only an outbound human link and is never scraped.

Available decisions include accepting current coordinates, entering corrected
coordinates, correcting the address, accepting the address or coordinates as
unknown, or leaving the issue unresolved. Numeric/global coordinate validation is
mandatory. Coordinates outside configured London bounds require a written override
reason. A manual geocode override is recorded explicitly; it never masquerades as
a provider result.

Address corrections preserve `address_original` and store the reviewed address
separately. Unless coordinates are explicitly supplied and validated in the same
decision, the old coordinates are cleared, the geocode becomes stale, and the
audit records that future geocoding is required. No Geoapify request is made.

## Price, AI, manual, and sublet review

Price controls preserve `price_text`, `price_numeric`, `price_period`, and
`price_monthly`. A reviewer may correct individual price fields, confirm the
current representation, or explicitly classify a missing value as ambiguous or
genuinely missing. Price corrections require evidence or a concise note.

AI/manual issues support accepting the current value, a corrected value, unknown,
or an unresolved draft. Sublet controls require an explicit true/false/unknown
choice; summer dates never become sublet evidence. Material corrections require
supporting text or a reviewer note.

Exclusion can be drafted only with a required reason. The existing application
workflow intentionally refuses exclusion until a separate canonical exclusion
policy exists, so an exclusion draft remains a visible manual safety gate. Bulk
exclusion and bulk coordinate/address correction are unavailable.

## Drafts, bulk actions, and application

Saving the form writes an audited decision, not canonical CSV data. Each record
has a stable content-derived ID, base issue ID, original/selected values, reviewer,
UTC time, evidence, note, current listing fingerprint, and narrowly validated
`apply_updates`. Writes are atomic and all-or-nothing. Identical saves are
idempotent. A conflicting replacement must explicitly supersede the active draft,
which remains in history.

Bulk preview is restricted to homogeneous allowlisted cases: deterministic
accepted-unknown classifications or already-resolved non-geocode current values.
It shows affected listing/issue counts, shared reason, fields, and evidence criteria
before a second explicit confirmation. Bulk exclusion and coordinate correction
are never offered.

Draft saving never applies decisions. After inspecting the decision artifact, use:

```powershell
.\.venv\Scripts\python.exe -m pipeline.operator apply-review-decisions `
  --run-id <run_id>

.\.venv\Scripts\python.exe -m pipeline.operator rebuild-canonical `
  --run-id <run_id>

.\.venv\Scripts\python.exe -m pipeline.operator review-auto `
  --run-id <run_id>

.\.venv\Scripts\python.exe -m pipeline.operator review-status `
  --run-id <run_id>
```

Application still uses the existing rollback, provenance, approval invalidation,
canonical rebuild, and idempotency rules. The dashboard displays operator status
but never decides that a run is approval-ready itself.

## Remote authoritative synchronization

The local copied bundle is a review workspace, not a second canonical source.
After saving local human decisions, explicitly merge only those human records into
the authoritative remote decision log:

```powershell
.\.venv\Scripts\python.exe -m pipeline.operator review-sync-decisions `
  --remote-host uwo-server `
  --run-id 20260801T045513214889Z_6e17c3f `
  --verbose
```

The operator requires normal Git/SSH preflight but skips Ollama and Geoapify. It
compares local and remote canonical SHA-256 values, uploads to a unique temporary
file, and asks the remote workflow to validate run ID, base decision, listing ID,
fingerprint, stable decision ID, reviewer, and conflicts before one atomic merge.
Automated records are not imported from the local file. The temporary upload is
removed afterward. Nothing is applied by synchronization.

Then apply and refresh the authoritative run explicitly:

```powershell
.\.venv\Scripts\python.exe -m pipeline.operator apply-review-decisions `
  --remote-host uwo-server `
  --run-id 20260801T045513214889Z_6e17c3f `
  --verbose

.\.venv\Scripts\python.exe -m pipeline.operator rebuild-canonical `
  --remote-host uwo-server `
  --run-id 20260801T045513214889Z_6e17c3f `
  --verbose

.\.venv\Scripts\python.exe -m pipeline.operator review-auto `
  --remote-host uwo-server `
  --run-id 20260801T045513214889Z_6e17c3f `
  --verbose

.\.venv\Scripts\python.exe -m pipeline.operator review-status `
  --remote-host uwo-server `
  --run-id 20260801T045513214889Z_6e17c3f `
  --verbose
```

## Security boundary

The default server binds only to `127.0.0.1`, has no CORS middleware, disables
interactive API documentation, sends no-store/CSP/frame/referrer/type headers,
serves only fixed assets, and accepts no browser file paths or commands. Listing
content is inserted with DOM text nodes, and source links are restricted to HTTP(S).
The API never loads `.env`, credentials, SSH keys, or database configuration.

A non-loopback `--host` is rejected unless `--unsafe-development-bind` is also
provided. That mode exposes sensitive listing evidence and mutable decisions to
the selected network and must not be used on an untrusted network.

Read-only mode disables individual and bulk writes:

```powershell
.\.venv\Scripts\python.exe -m pipeline.operator review-ui `
  --run-id <run_id> `
  --read-only
```

Approval and database import remain separate, explicit workflows after all
operator-calculated blockers are cleared.
