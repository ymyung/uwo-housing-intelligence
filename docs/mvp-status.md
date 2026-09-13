# Working MVP status

This document records the repository baseline as of 2026-08-12. Detailed
contracts remain in the linked subsystem documents.

## Currently working

- The versioned Stage 0-3 pipeline discovers UWO advertisements, extracts
  structured website data, applies deterministic normalization before AI
  enrichment, records review queues, geocodes with caching, and produces a
  reviewed canonical artifact. Approval remains an explicit gate.
- Stage 4 imports one approved run into normalized PostgreSQL tables. Stable
  advertisements, canonical properties, immutable observations, run history,
  lifecycle state, provenance, and review evidence remain separate.
- Ranking v1 is computed at listing grain, persisted with versioned inputs and
  explanations, and exposed through API filters/sorts and the React interface.
- Listing search, composable dropdown filters, pagination, saved comparisons,
  detail selection, and the Leaflet listing map are integrated.
- Versioned City of London address, building, and parcel snapshots can be
  imported into PostGIS and matched conservatively as shadow evidence.
- OTP is the exact-route authority. Normalized walking, cycling, and six-period
  transit profiles retain compact route geometry and schedule provenance.
- R5 provides accepted walking-only numerical travel-time surfaces. Compact
  property-based surface artifacts are cached in PostgreSQL with explicit
  network, grid, algorithm, and engine identities.
- The Leaflet walking numerical map renders approximate minute cells. A valid
  clicked destination can be handed to OTP for an exact walking route without
  persisting the arbitrary click.

## P0 canonical-data baseline

- The approved canonical run `20260812T075717937851Z_ea35b07` applies the
  bounded deterministic P0 source-semantic corrections without new AI calls.
  It recovers one explicit civic address and geocodes it through the normal
  cached provider workflow; genuinely missing source addresses remain unknown.
- City of London reference data remains shadow evidence. It is not promoted
  over provider coordinates or used for fuzzy property remediation.
- Location-risk dispositions remain explicit: a bounded trustworthy subset is
  suitable for map and route demonstration, while uncertain locations can be
  withheld. Further source-completeness work is post-MVP.

## Current intentional limitations

- City reference matches do not automatically replace current Geoapify
  coordinates or merge properties; promotion requires a separate reviewed
  policy and remediation milestone.
- Bicycle and transit numerical surfaces are disabled because they did not
  pass the accepted R5 surface contract. OTP remains available for exact
  supported routes.
- Ranking has no POI or neighbourhood-amenity component yet. The amenity score
  is null and its configured weight is zero.
- Only the reviewed Western main-campus destination is active. Static GTFS
  estimates are not live vehicle, delay, cancellation, or detour information.
- There is no public production Western-authorized listing feed or partnership
  integration in this repository.
- Accounts, alerts, moderation, analytics, sponsorship, and paid ranking are
  outside this MVP.

## Local startup

Create the ignored `config/local-dev.env` from the documented example and
provide the ignored routing inputs described in `docs/local-development.md`.
Do not place credentials in source or command history.

```powershell
.\scripts\dev.ps1 start
.\scripts\serve_local_api.ps1
```

In another terminal:

```powershell
Set-Location frontend
npm run dev
```

The canonical services are loopback-bound. `dev.ps1 stop` preserves the local
PostgreSQL volume and routing inputs.

## Next MVP and partnership priorities

1. Establish a Western partnership and an authorized, stable listing-feed
   contract, including access, privacy, attribution, and operational ownership.
2. Define and validate a reviewable City-coordinate promotion/remediation
   policy without weakening current geocode provenance.
3. Add a separately versioned POI/amenity data foundation before enabling an
   amenity ranking component.
