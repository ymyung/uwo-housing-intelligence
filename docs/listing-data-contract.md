# Listing data contract

This contract governs student-facing listing facts from Western Off-Campus
Housing. It applies from Stage 1 parsing through the API and frontend.

## Evidence priority and provenance

Use evidence in this order:

1. a structured field captured from the Western listing;
2. a deterministic parse of explicit source text;
3. AI only when the first two sources leave a field unresolved;
4. an explicit manual-review decision.

Stage 1 retains both `*_raw` values and normalized/rule values. Stage 2 must
not replace a resolved rule value. AI evidence must be a verbatim source
snippet and must explicitly support the proposed meaning. Stage 4 retains raw,
provenance, confidence, evidence, and review metadata in each observation.
Manual review may supersede a deterministic rule only as an explicit,
auditable decision.

## Operational field rules

- Identity: the Western numeric listing ID and canonical details URL identify
  an advertisement. A normalized address may associate advertisements with a
  property, but never deduplicates separate ads or rooms by itself.
- Address: retain the captured address and its normalized property candidate.
  Geocoding is location enrichment, not source authority for the listing text.
- Price: retain the original amount and period text. `price_monthly` is derived
  exactly once: monthly values are unchanged, weekly values use `52 / 12`, and
  daily values use `365 / 12`. An unknown period yields an unknown monthly price.
- Gender: Western's structured `Preferred Gender: Male/Female` means
  `male_preferred`/`female_preferred`, not exclusion. `male_only` or
  `female_only` requires separate explicit “only” wording. No source statement
  yields `not_specified`; explicit no-preference wording yields `any`.
- Sublet: `true` requires an explicit Sublet source category or explicit
  sublet, sublease, takeover, or transfer wording. Silence remains unknown.
- Summer: an explicit closed advertised interval is `summer_available` when it
  intersects May–August and `non_summer` when it is wholly outside those months.
  Closed yearless ranges use cyclic month membership without inventing years;
  open-ended or ambiguous ranges remain unknown. Structured availability wins
  over description parsing, with contradictory evidence flagged for review.
  May–August, short duration, and student context never imply sublet status.
- Housing type: preserve meaningful Western categories such as house/apartment
  to share, bachelor apartment, room, and sublet instead of collapsing them.
- Lease: preserve the raw source term separately. Deterministic duration bands
  may populate the normalized type; availability dates do not determine it.
  A zero-month value is unknown, not a real lease duration.
- Furnished: use an explicit furnishing amenity or clear furnished/unfurnished
  wording. Mentions of unrelated furniture or silence stay unknown.
- Utilities: Western's structured Included/Extra value wins. `all_included`,
  `partially_included`, and `not_included` stay distinct. Partial or missing
  information does not become a false all-utilities-included boolean.
- Bathrooms: structured private/shared amenities win. Counts or types inferred
  from contextual prose remain reviewable unless the source is explicit.

## Review policy

Unknown is not false, not specified is not “no,” preferred is not “only,” and
summer is not sublet. Conflicts and contextual language remain in the review
queue; the pipeline must not manufacture certainty to reduce review counts.
