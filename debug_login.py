import os
import pyotp
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
EMAIL = os.getenv("LOGIN_GOV_EMAIL")
PASSWORD = os.getenv("LOGIN_GOV_PASSWORD")
TOTP_SECRET = os.getenv("LOGIN_GOV_TOTP_SECRET")
START_URL = "https://www.employeeexpress.gov/ELS"

# This folder will store the browser fingerprint and cookies
PROFILE_DIR = "./playwright_profile"

def run_debug():
    with sync_playwright() as p:
        print("Launching persistent browser profile...")
        
        # We use launch_persistent_context instead of launch
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            slow_mo=500,
            # These arguments help hide the automation from security checks
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox"
            ],
            # Spoof a standard Chrome user agent
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        
        # Persistent contexts automatically open one blank page
        page = context.pages[0] if context.pages else context.new_page()

        print(f"Navigating to {START_URL}...")
        page.goto(START_URL)

        print("Looking for Login.gov button...")
        login_btn = page.locator('a[title="Sign in with Login.gov"]')
        if login_btn.is_visible():
            print("Found button. Clicking...")
            login_btn.click()
        else:
            print("Login button not found! It might already be logged in, or the page changed.")
            page.pause()

        print("Waiting for Login.gov email field...")
        try:
            page.wait_for_selector('input[type="email"]', timeout=10000)
            page.fill('input[type="email"]', EMAIL)
            page.fill('input[type="password"]', PASSWORD)
            page.click('button:has-text("Sign in"), button[type="submit"]')
        except Exception as e:
            print(f"Failed at credentials step. You may already be logged in. Error: {e}")
            page.pause()

        # Check if we hit the Security Warning page
        if page.locator('text="Security check failed"').is_visible() or page.locator('text="We don’t recognize the device"').is_visible():
            print("\n*** SECURITY BLOCK DETECTED ***")
            print("Please use the open browser window to manually log in right now.")
            print("Once you successfully reach the Employee Express paystub page, click 'Resume' in the Playwright Inspector.")
            page.pause()

        print("Waiting for 2FA screen...")
        try:
            # If we manually logged in during the pause, this might timeout, which is fine.
            if page.locator('input[name="code"], input[id="code"]').is_visible():
                print("Generating and submitting TOTP code...")
                totp = pyotp.TOTP(TOTP_SECRET)
                page.fill('input[name="code"], input[id="code"]', totp.now())
                page.click('button:has-text("Submit"), button[type="submit"]')
        except Exception as e:
            print(f"Failed at 2FA step (might have been bypassed manually): {e}")

        print("Waiting to return to Employee Express (ELS dropdown)...")
        try:
            page.wait_for_selector('#ddlELS', timeout=30000)
            print("Success! Reached the paystubs page.")
            page.pause()
        except Exception as e:
            print(f"Failed to reach final ELS page: {e}")
            page.pause()

        context.close()

if __name__ == "__main__":
    run_debug()
