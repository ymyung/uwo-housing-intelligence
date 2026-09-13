# Persisted Ranking v1 API contract

## Boundary

The housing API reads precomputed Ranking v1 rows. Request handlers never
extract ranking features, calculate market cohorts, call accessibility
providers, or recompute scores. The authoritative path is:

```text
ranked_housing_listings view
  -> listing repository query
  -> whitelisted ranking projection
  -> collection/detail JSON
```

`ranked_housing_listings` preserves the established active-listing columns and
left-joins only the row where `ranking_version = 'ranking-v1'` and
`is_current = true`. The database's partial unique index prevents more than one
current row for a listing/version. Historical scores and other ranking versions
cannot duplicate or replace the API result.

The old `backend/ranking.py` module is a compatibility prototype. It is not
called by the API and is not an alternative Ranking v1 source.

## Listing response

Collection and detail responses add one backward-compatible `ranking` field.
It is null when no current Ranking v1 row exists. Otherwise its shape is:

```json
{
  "version": "ranking-v1",
  "status": "ranked",
  "overall_score": 61.42,
  "components": {
    "value": 68.2,
    "campus_access": 57.1,
    "transit": 62.0,
    "amenities": null,
    "data_quality": null
  },
  "weights": {
    "value": 0.45,
    "campus_access": 0.35,
    "transit": 0.2,
    "amenities": 0.0
  },
  "signals": {
    "value": {},
    "campus_access": {},
    "transit_periods": []
  },
  "reasons": [
    {
      "kind": "value",
      "title": "Good value",
      "detail": "13% below the comparison median"
    },
    {
      "kind": "campus_access",
      "title": "Campus commute",
      "detail": "18 min walk to Western"
    }
  ],
  "warnings": {
    "accessibility_reason_codes": [],
    "accessibility_review_required": false,
    "listing_review_flags": []
  },
  "eligibility_reasons": [],
  "unavailable_components": ["amenities"],
  "amenity_status": "not_implemented",
  "data_quality_status": "not_scored",
  "computed_at": "2026-08-09T00:00:00+00:00",
  "input_fingerprint": "..."
}
```

Scores are JSON numbers or null and remain bounded by the persistence
constraints. Decimal database values are converted safely. The API whitelists
cohort statistics, walking/cycling signals, transit-period summaries, reason
codes, and review flags; it does not return raw routing payloads, configuration
internals, database URLs, or credentials.

Statuses retain their persisted meaning:

- `ranked`: overall and required component scores are present.
- `partial`: useful deterministic evidence may be present, but overall is null.
- `excluded`: overall is null because eligibility was not met.

Amenities remain null with `amenity_status = "not_implemented"`. Null never
means a score of zero.

`reasons` is a small student-facing projection of the same persisted signals;
it is not another ranking calculation. A value reason requires the stored
comparison count and price delta, campus text requires stored walking minutes,
and transit text requires the stored weekday-morning representative duration.
Missing evidence produces no claim. Partial rankings may still expose factual
available reasons while retaining a null overall score; excluded listings are
shown as not ranked rather than receiving a negative reason.

## Collection queries

The existing default query name `recommended` is preserved but now means the
authoritative persisted overall score descending. This replaces the legacy
request-time prototype without introducing a second ranking result. Existing
non-ranking sorts remain available.

Supported `sort` values are:

- `recommended`
- `overall_score`
- `value_score`
- `campus_access_score`
- `transit_score`
- `price_low`
- `price_high`
- `distance`
- `newest`

`order=asc|desc` may override the sort's established direction. Ranking sorts
default to descending. Null values always sort last in both directions, and
`listing_id` ascending is the stable tie-breaker.

Ranking filters are:

- `ranking_status=ranked|partial|excluded`
- `min_score` and `max_score`
- `min_value_score`
- `min_campus_access_score`
- `min_transit_score`

All score filters accept 0–100. `min_score`/`max_score` operate only on non-null
overall scores, so partial and excluded rows cannot masquerade as zero-scored
ranked rows. Ranking filters compose with existing listing filters.

Database-capable repositories apply filters and ordering before `offset` and
`limit`. PostgreSQL performs a count query plus one bounded page query; it does
not issue one ranking query per listing. Supabase applies the same operations to
the read-only projection through PostgREST. Offline fixture repositories retain
an equivalent in-memory path for tests and demos.

## Local validation

With the documented local environment initialized:

```powershell
.\scripts\dev.ps1 start
.\.venv\Scripts\python.exe -m uvicorn backend.main:app `
  --host 127.0.0.1 --port 8000
Invoke-RestMethod `
  'http://127.0.0.1:8000/api/listings?ranking_status=ranked&sort=overall_score&page_size=5'
```

The API process must receive the local `ACCESSIBILITY_DATABASE_URL` or
`DATABASE_URL`. Do not place credentials in source or command history.
