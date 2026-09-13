# Screenshot Capture Guide

Capture all three images from the local fixture-backed application. Do not run the scraper, AI enrichment, geocoder, database importer, OTP, R5, Supabase, or any hosted API for these screenshots.

## Use the Synthetic Accessibility Fixture

From the repository root, start only the local fixture API:

```powershell
$env:HOUSING_FIXTURE_CSV = 'tests/fixtures/accessibility_demo/listings.csv'
$env:ACCESSIBILITY_FIXTURE_PATH = 'tests/fixtures/accessibility_demo/profiles.json'
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8000
```

In a second terminal:

```powershell
Set-Location frontend
npm run dev
```

With both local servers running, capture and verify all three states from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\capture_readme_screenshots.py
```

The capture script fixes the browser viewport at 1440 × 900 and fails if the page attempts any non-loopback HTTP request.

The fixture contains invented records such as “Exact profile demo” and “Same-stop transit demo.” Do not substitute real listings. Do not enable live routing or geocoding. If the configured basemap would contact an unapproved tile service, use an approved local tile configuration or disable the basemap before capture.

## Consistent Framing

Use these settings for all three images:

- desktop browser content viewport: approximately 1440 × 900 pixels;
- browser zoom: 100%;
- default light theme and normal application spacing;
- crop out the URL bar, bookmarks, browser profile, desktop taskbar, terminals, and developer tools;
- retain the complete application header and map attribution when the map is visible;
- close notifications, tooltips, debug panels, and unrelated overlays;
- export PNG files at native resolution without decorative device frames;
- confirm that every visible title and address contains fixture/demo wording.

## 1. Primary Hero Screenshot

**Filename:** `docs/images/hero-overview.png`

Show:

- the application name/header;
- the main filter controls;
- the listing-results column with at least three synthetic cards;
- the map and its synthetic markers;
- one clearly visible price/ranking summary;
- no listing detail drawer obscuring the overall layout.

Frame the full application viewport rather than a full-page scrolling capture. Balance the listing column and map so a recruiter can understand the product immediately.

**README placement:** replace the pending line directly below `### Product overview` with:

```markdown
![UWO Housing Intelligence discovery map with synthetic listings](docs/images/hero-overview.png)
```

## 2. Filtered Search State

**Filename:** `docs/images/filtered-search.png`

Start from a clean page, then apply:

- Bedrooms: `2`;
- Maximum monthly rent: `1000`.

The result should isolate the synthetic “Same-stop transit demo” while excluding the $1100 fallback record. Show the active filter values, result count, matching card, and corresponding map state. Keep the same viewport and crop as the hero image.

**README placement:** replace the pending line directly below `### Filtered search` with:

```markdown
![Filtered two-bedroom synthetic housing search](docs/images/filtered-search.png)
```

## 3. Listing and Transit Intelligence

**Filename:** `docs/images/listing-intelligence.png`

Open the synthetic “Same-stop transit demo” listing. Show as much of the following in one uncluttered viewport as the interface supports:

- listing title and monthly price;
- bedroom and housing-type details;
- explainable ranking or comparison evidence;
- “Getting to Western” transportation information;
- the same-stop estimate label, typical duration/range, and any visible freshness or confidence language;
- the selected marker or map context, if it remains visible.

Do not request a new route. The screenshot must use the persisted synthetic accessibility fixture only. Prefer the detail panel over an excessively zoomed map.

**README placement:** replace the pending line directly below `### Listing and transit intelligence` with:

```markdown
![Synthetic listing detail with transit accessibility evidence](docs/images/listing-intelligence.png)
```

## Final Privacy Check

Before adding each image, inspect it at full resolution and confirm that it contains no:

- real address, listing photo, landlord, phone number, or email;
- API key, token, database URL, terminal, or developer console;
- personal browser profile, bookmarks, extensions, notifications, or local filesystem paths;
- unrelated tabs or applications.

After adding the PNGs, run `git status --short` and verify that only the three intended files appear under `docs/images/`.
