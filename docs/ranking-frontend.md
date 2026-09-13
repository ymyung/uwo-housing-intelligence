# Ranking v1 frontend

## Product boundary

The frontend treats the nested `listing.ranking` object as the only ranking
authority. It formats persisted API values for display; it does not apply
weights, calculate components, build comparison cohorts, call routing
providers, or fall back to the legacy `backend/ranking.py` prototype.

Both listing cards and the listing detail panel use
`frontend/src/domain/ranking.js`. This keeps status, score, warning, and signal
language consistent. Raw numeric API values remain unchanged in application
state. Visible scores use one presentation rule: round to the nearest whole
number and append `/100`.

## States and components

- `ranked`: show `Overall match`, the overall score, and available Value,
  Campus access, and Transit components.
- `partial`: show `Partial ranking data`, never an overall score, and any
  available component evidence.
- `excluded`: show `Not enough verified data to rank`, never an overall score,
  and translated eligibility reasons in the detail panel.
- `ranking = null` or an unknown status: show `Ranking unavailable` without a
  zero or `NaN` placeholder.

Amenities are omitted from compact cards. The detail panel says `Not available
yet` when `amenity_status` is `not_implemented`; null is never rendered as zero
and does not mean the advertisement lacks amenities.

## Explanations and evidence notes

Cards pair the overall state with at most one concise persisted reason. The
detail panel leads with **Why this listing?** and the API-provided value,
Western walking, and weekday-morning transit reasons. Raw component scores are
not used as student-facing reasons. Detailed persisted evidence remains in an
optional disclosure for auditability.

The `More ranking evidence` disclosure formats only structured API signals:

- monthly price, comparison median, and the persisted price delta;
- walking and cycling minutes;
- named transit periods, persisted period score, representative minutes,
  transfers, and reason codes.

Known accessibility findings are translated into restrained student-facing
notes. They remain evidence limitations, not provider failures. Listing and
accessibility review flags are also disclosed without exposing raw routing
payloads or internal fingerprints.

## Sorts, filters, and pagination

The existing `recommended` default is retained. The backend defines it as
persisted Ranking v1 overall score descending. The UI also exposes the server
sort keys `overall_score`, `value_score`, `campus_access_score`, and
`transit_score`, alongside the pre-existing price, distance, and newest sorts.

Ranking v1 adds two restrained filters under **More filters**:

- ranking availability (`ranking_status`);
- minimum overall match (`min_score`) using 50/60/70/80 presets.

Component minimums remain an API capability but are intentionally deferred to
avoid an oversized filter panel. Ranking parameters use the same central query
builder as price, bedrooms, housing, lease, and other filters. They are sent to
the collection endpoint, so filtering and ordering occur before server-side
pagination. Changing a filter, sort, destination, saved-list view, or page
clears the current selection and prevents stale card/map details.

## Accessibility and responsive behavior

Ranking status is communicated with text and numbers rather than color alone.
Controls remain native labeled selects and buttons. The explanation uses native
`details`/`summary`, which provides keyboard disclosure semantics without a
custom click target. At narrow widths, detail components become one column and
compact card components wrap instead of overflowing.

## Local workflow

Start the canonical local services and API using the repository workflow:

```powershell
.\scripts\dev.ps1 start
.\scripts\serve_local_api.ps1
```

Then run the frontend from `frontend/` with `npm run dev`. Local validation
artifacts belong under ignored `data/ranking-frontend-validation/`. Stop the API
process and run `.\scripts\dev.ps1 stop` when finished.
