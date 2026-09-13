import html
import pandas as pd
from pathlib import Path

input_csv = Path(r"data\processed\stage5_otp_travel_times_with_images.csv")
output_html = Path(r"data\processed\image_review.html")

df = pd.read_csv(input_csv)
df = df[df["image_url"].notna()].copy()

rows = []

for _, r in df.head(200).iterrows():
    img = html.escape(str(r.get("image_url", "")))
    addr = html.escape(str(r.get("address", "")))
    lid = html.escape(str(r.get("listing_id", "")))
    url = html.escape(str(r.get("listing_url", "")))
    count = html.escape(str(r.get("image_scrape_count", "")))

    rows.append(f"""
    <div class="card">
      <img src="{img}" loading="lazy" onerror="this.style.display='none'">
      <h3>{addr}</h3>
      <p>ID: {lid}</p>
      <p>Image count: {count}</p>
      <a href="{url}" target="_blank">Original listing</a>
      <p class="url">{img}</p>
    </div>
    """)

page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Listing Image Review</title>
  <style>
    body {{
      font-family: Segoe UI, Arial, sans-serif;
      padding: 24px;
      background: #f6f3fa;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 18px;
    }}
    .card {{
      background: white;
      border: 1px solid #ddd;
      border-radius: 16px;
      padding: 12px;
      box-shadow: 0 6px 18px #0001;
    }}
    .card img {{
      width: 100%;
      height: 180px;
      object-fit: cover;
      border-radius: 12px;
      background: #eee;
    }}
    .card h3 {{
      font-size: 15px;
    }}
    .url {{
      font-size: 11px;
      color: #777;
      word-break: break-all;
    }}
  </style>
</head>
<body>
  <h1>Listing Image Review</h1>
  <p>Showing up to 200 listings with image_url.</p>
  <p>Total listings with images: {len(df)}</p>
  <div class="grid">
    {''.join(rows)}
  </div>
</body>
</html>
"""

output_html.write_text(page, encoding="utf-8")

print(f"Saved gallery to {output_html}")
print(f"Listings with images: {len(df)}")
