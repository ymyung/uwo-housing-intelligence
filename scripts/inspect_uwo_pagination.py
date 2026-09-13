from playwright.sync_api import sync_playwright
from pathlib import Path
import re

URL = "https://offcampus.uwo.ca/listings/"

def main():
    output_dir = Path("debug")
    output_dir.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})

        print(f"Opening {URL}")
        page.goto(URL, wait_until="networkidle", timeout=60000)

        print("\n=== PAGE TITLE ===")
        print(page.title())

        print("\n=== CURRENT URL ===")
        print(page.url)

        html = page.content()
        (output_dir / "uwo_listings_rendered.html").write_text(html, encoding="utf-8")
        print("\nSaved rendered HTML to debug/uwo_listings_rendered.html")

        print("\n=== LINKS THAT LOOK LIKE LISTINGS/PAGINATION ===")
        anchors = page.locator("a[href]").all()

        for a in anchors:
            text = (a.inner_text(timeout=1000) or "").strip().replace("\n", " ")
            href = a.get_attribute("href") or ""

            if (
                "/Listings/Details/" in href
                or "listing" in href.lower()
                or text.isdigit()
                or text in {"Next", "Previous", "»", "«", ">", "<"}
            ):
                print(f"TEXT={text!r} | HREF={href!r}")

        print("\n=== BUTTONS ===")
        buttons = page.locator("button").all()
        for b in buttons:
            try:
                text = (b.inner_text(timeout=1000) or "").strip().replace("\n", " ")
                print(f"BUTTON TEXT={text!r}")
            except Exception:
                pass

        print("\n=== SELECT DROPDOWNS ===")
        selects = page.locator("select").all()
        for i, s in enumerate(selects):
            try:
                name = s.get_attribute("name")
                id_ = s.get_attribute("id")
                print(f"SELECT {i}: name={name!r}, id={id_!r}")
            except Exception:
                pass

        print("\n=== FORMS ===")
        forms = page.locator("form").all()
        for i, f in enumerate(forms):
            try:
                action = f.get_attribute("action")
                method = f.get_attribute("method")
                print(f"FORM {i}: method={method!r}, action={action!r}")
            except Exception:
                pass

        input("\nBrowser is open. Manually click page 2 / next if you see it, then press Enter here... ")

        print("\n=== AFTER MANUAL CLICK ===")
        print("Current URL:", page.url)

        html2 = page.content()
        (output_dir / "uwo_listings_after_click.html").write_text(html2, encoding="utf-8")
        print("Saved after-click HTML to debug/uwo_listings_after_click.html")

        browser.close()

if __name__ == "__main__":
    main()