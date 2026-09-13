# Walking travel-time surfaces

The product supports a compact **walking-only** R5 numerical surface. This is
not an exact route: OTP remains the authoritative provider for route details,
geometry, and all bicycle/transit requests. See
`docs/travel-time-surface-contract.md` for the benchmark evidence.

## Architecture

The FastAPI API resolves a listing to its canonical property and trusted
coordinates. It then reads or claims one compact PostgreSQL surface artifact.
On a cache miss it calls the loopback-only `r5-surface` container, which loads
the accepted R5 network once at startup and accepts only walking requests.
The vector is compressed and stored as one row, not 14,803 rows.

The reviewed policy is `config/travel-time-surface.example.toml`: 200 m
EPSG:26917 row-major grid, 14,803 cells, 400 m maximum destination snap,
50 m maximum origin snap, 120-minute cap, seconds encoded little-endian, and
`65535` as unavailable. A separate bitset records whether a cell was rejected
for destination snap quality rather than genuinely unreachable.

## Cache and invalidation

Identity includes canonical property ID, exact origin coordinates, grid
fingerprint, network/OSM fingerprint, R5/r5py versions, walking configuration,
and algorithm version. It intentionally excludes GTFS because walking analysis
does not use a schedule. A changed OSM/network, R5 version, grid, origin, or
walking policy creates a new historical-compatible cache identity; old rows
remain auditable.

## API

- `GET /api/travel-time-surfaces/grid` returns deterministic grid metadata.
- `GET /api/listings/{listing_id}/travel-time-surface?mode=walking` returns
  metadata and lazily computes a missing compatible surface.
- `GET /api/travel-time-surfaces/{surface_id}/values` returns gzip-compressed
  `application/octet-stream` uint16 little-endian values with an ETag.

`mode=bicycle` and `mode=transit` return a documented 422 response. Vectors
are intentionally absent from listing collection/detail endpoints.

## Frontend map mode

The listing map has a walking-only **Walk time** mode once a listing is
selected. It fetches the grid once per browser session, then caches decoded
surface values and destination-validity bits by surface ID and grid fingerprint.
The binary vector is decoded as little-endian `Uint16Array`; browser `fetch`
already transparently handles the gzip content encoding. A single Leaflet-pane
canvas projects the EPSG:26917 grid and draws visible valid cells, sparse
minute labels at high zoom, a compact legend, and hover/click estimates.

The map always says *Approximate travel time* / *Approx. walk*; rejected or
unavailable cells are not rendered as long trips. A clicked point is retained
as the student’s actual coordinate. **Show exact route** calls the local OTP
walking endpoint from the listing’s trusted canonical-property origin to that
clicked coordinate. Its geometry, duration, and distance are shown as *Exact
walking route*, while the canvas estimate remains explicitly approximate.
No-route or OTP failures leave the cached surface available and show a concise
route-unavailable state; arbitrary clicks are not persisted.

## Local operation

Start the local stack with `./scripts/dev.ps1 start`; the R5 process is
loopback-bound on port 8091. Use:

```powershell
python -m scripts.walking_surface status --property-id 4
python -m scripts.walking_surface compute --property-id 4
```

The database has a unique cache identity plus a PostgreSQL advisory lock. A
concurrent request observes `computing` rather than starting duplicate R5 work;
failed rows are retryable. Stop temporary services with `./scripts/dev.ps1 stop`.

## Limitations

Only walking passed acceptance. Full-cohort precompute and ranking input are
not implemented. Bicycle and transit numerical surfaces remain deliberately
disabled; exact routes for those modes continue to belong to OTP.
