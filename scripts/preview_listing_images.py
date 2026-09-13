import argparse
from pathlib import Path
import html

import pandas as pd


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input-csv",
        type=Path,
        default=Path("data/processed/stage5_images_playwright_test.csv"),
    )
    parser.add_argument(
        "--output-html",
        type=Path,
        default=Path("data/processed/image_preview.html"),
    )
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    df = pd.read_csv(args.input_csv)

    rows = df[df["image_url"].notna()].head(args.limit)

    cards = []

    for _, row in rows.iterrows():
        listing_id = html.escape(str(row.get("listing_id", "")))
        address = html.escape(str(row.get("address", "")))
        image_url = html.escape(str(row.get("image_url", "")))
        listing_url = html.escape(str(row.get("listing_url", "")))
        count = html.escape(str(row.get("image_scrape_count", "")))

        cards.append(f"""
        <div class="card">
          <img src="{image_url}" loading="lazy" />
          <div class="info">
            <h3>{address}</h3>
            <p><strong>ID:</strong> {listing_id}</p>
            <p><strong>Images found:</strong> {count}</p>
            <a href="{image_url}" target="_blank">Open image</a>
            <a href="{listing_url}" target="_blank">Open listing</a>
          </div>
        </div>
        """)

    page = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <title>Listing Image Preview</title>
      <style>
        body {{
          margin: 0;
          padding: 24px;
          font-family: Arial, sans-serif;
          background: #f6f3fa;
        }}
        h1 {{
          margin-bottom: 20px;
        }}
        .grid {{
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
          gap: 18px;
        }}
        .card {{
          background: white;
          border-radius: 16px;
          overflow: hidden;
          box-shadow: 0 8px 24px rgba(0,0,0,0.08);
        }}
        img {{
          width: 100%;
          height: 190px;
          object-fit: cover;
          background: #eee;
        }}
        .info {{
          padding: 14px;
        }}
        h3 {{
          margin: 0 0 8px;
          font-size: 16px;
        }}
        p {{
          margin: 4px 0;
          color: #555;
        }}
        a {{
          display: inline-block;
          margin-top: 8px;
          margin-right: 10px;
          color: #4f2683;
          font-weight: bold;
        }}
      </style>
    </head>
    <body>
      <h1>Listing Image Preview</h1>
      <p>Showing {len(rows)} listings with image_url.</p>
      <div class="grid">
        {''.join(cards)}
      </div>
    </body>
    </html>
    """

    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.write_text(page, encoding="utf-8")

    print(f"Saved preview → {args.output_html}")


if __name__ == "__main__":
    main()