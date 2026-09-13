from playwright.sync_api import sync_playwright
from pathlib import Path

URL = "https://offcampus.uwo.ca/listings/"

def main():
    Path("debug").mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})

        def log_request(request):
            if "Listings" in request.url or "listings" in request.url:
                print("\n=== REQUEST ===")
                print("METHOD:", request.method)
                print("URL:", request.url)
                post_data = request.post_data
                if post_data:
                    print("POST DATA:")
                    print(post_data[:2000])

        def log_response(response):
            if "Listings" in response.url or "listings" in response.url:
                print("\n=== RESPONSE ===")
                print("STATUS:", response.status)
                print("URL:", response.url)

        page.on("request", log_request)
        page.on("response", log_response)

        print(f"Opening {URL}")
        page.goto(URL, wait_until="networkidle", timeout=60000)

        print("""
Browser is open.

Do this manually in the browser:
1. Pick a filter, for example Location = Downtown
2. Click Search
3. Try clicking Price / Distance / Location sort buttons too
4. Watch the PowerShell output for REQUEST / POST DATA

After testing, press Enter here.
""")

        input("Press Enter to save final HTML and close...")

        html = page.content()
        Path("debug/uwo_network_after_actions.html").write_text(html, encoding="utf-8")

        print("Saved debug/uwo_network_after_actions.html")
        browser.close()

if __name__ == "__main__":
    main()