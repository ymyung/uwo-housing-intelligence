# Synthetic Sample Data

`listings.demo.csv` contains three invented records for schema exploration and local UI development. The people, listing URLs, addresses, and descriptions are fictional; coordinates are approximate demonstration points and must not be interpreted as available housing.

Run the fixture-backed API from the repository root:

```powershell
$env:HOUSING_FIXTURE_CSV = 'data/samples/listings.demo.csv'
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8000
```

This sample does not provide persisted accessibility profiles, exact routing, or listing history. The richer fixtures under `tests/fixtures/` exist for automated tests.
