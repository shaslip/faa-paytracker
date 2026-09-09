import os
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
START_URL = "https://www.employeeexpress.gov/ELS"

def run_debug():
    with sync_playwright() as p:
        # headless=False makes the browser visible
        # slow_mo=500 slows down every action by half a second so you can watch
        browser = p.chromium.launch(headless=False, slow_mo=500)
        context = browser.new_context()
        page = context.new_page()

        print(f"Navigating to {START_URL}...")
        page.goto(START_URL)

        print(f"Current URL after load: {page.url}")
        
        # This will freeze the script and open the Playwright Inspector tool.
        # Look at the browser window. If there is a "Warning" banner or an extra button,
        # note what it says. You can click "Resume" in the inspector to continue the script.
        print("Pausing... Check the browser window. Click 'Resume' in the Playwright Inspector to continue.")
        page.pause()

        # Check for the Login.gov button
        if page.locator('text="Sign in with Login.gov"').is_visible():
            print("Found 'Sign in with Login.gov' button. Clicking...")
            page.click('text="Sign in with Login.gov"')
        else:
            print("Could not find 'Sign in with Login.gov' text on this page.")

        print("Waiting for email field...")
        try:
            page.wait_for_selector('input[type="email"]', timeout=10000)
            print("Success! Found the email field.")
        except Exception as e:
            print("Timeout! Could not find the email field.")
            print("Pausing again so you can inspect the current page...")
            page.pause()

        browser.close()

if __name__ == "__main__":
    run_debug()
