# R5/r5py proof of concept

This Docker image is an isolated travel-time-surface experiment. It is not a
development-stack service and it does not replace OTP's exact routing API.

The image uses Temurin JDK 21 and pinned `r5py==1.1.7`. It reads `data/routing`
read-only and writes only ignored artifacts under
`data/travel-time-surface-validation/r5`.

The experiment's deterministic buffered London extract was created with:

```powershell
docker compose -f docker-compose.r5-poc.yml run --rm --entrypoint osmium r5-poc `
  extract --strategy complete_ways -b -81.43000,42.80000,-81.07000,43.10000 `
  /inputs/ontario.osm.pbf -o /cache/london-area-buffered.osm.pbf --overwrite
```

The buffer deliberately extends beyond the benchmark grid. The source Ontario
PBF is never modified; both source and derived SHA-256 values are written to
the ignored POC environment artifact.
