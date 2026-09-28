import os
import time
import pyotp
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
EMAIL = os.getenv("LOGIN_GOV_EMAIL")
PASSWORD = os.getenv("LOGIN_GOV_PASSWORD")
TOTP_SECRET = os.getenv("LOGIN_GOV_TOTP_SECRET")
START_URL = "https://www.employeeexpress.gov/ELS"

PROFILE_DIR = "./playwright_profile"

def run_debug():
    with sync_playwright() as p:
        print("Launching persistent browser profile...")
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            slow_mo=500,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox"
            ],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        
        page = context.pages[0] if context.pages else context.new_page()

        print(f"Navigating to {START_URL}...")
        page.goto(START_URL)
        time.sleep(3) # Human delay

        print("Looking for Login.gov button...")
        login_btn = page.locator('a[title="Sign in with Login.gov"]')
        if login_btn.is_visible():
            print("Found button. Clicking...")
            login_btn.click()
            time.sleep(3) # Wait for redirect
        else:
            print("Login button not found! Might already be logged in.")

        print("Waiting for Login.gov email field...")
        try:
            page.wait_for_selector('input[type="email"]', timeout=10000)
            page.fill('input[type="email"]', EMAIL)
            page.fill('input[type="password"]', PASSWORD)
            time.sleep(2) # Pause before submitting
            page.click('button:has-text("Sign in"), button[type="submit"]')
            time.sleep(3) # Wait for 2FA page load
        except Exception as e:
            print(f"Skipping credentials step (may already be logged in).")

        # Security check block
        if page.locator('text="Security check failed"').is_visible() or page.locator('text="We don’t recognize the device"').is_visible():
            print("\n*** SECURITY BLOCK DETECTED ***")
            print("Please log in manually in the browser. Click 'Resume' in the inspector when done.")
            page.pause()

        print("Waiting for 2FA screen...")
        try:
            # Using the stable autocomplete attribute instead of dynamic ID
            totp_input = page.locator('input[autocomplete="one-time-code"]')
            if totp_input.is_visible():
                print("Generating and submitting TOTP code...")
                totp = pyotp.TOTP(TOTP_SECRET)
                totp_input.fill(totp.now())
                time.sleep(2) # Pause before submitting
                page.click('button:has-text("Submit"), button[type="submit"]')
                time.sleep(3) # Wait for redirect back to EEX
        except Exception as e:
            print(f"Failed at 2FA step: {e}")

        print("Waiting to return to Employee Express (ELS dropdown)...")
        try:
            page.wait_for_selector('#ddlELS', timeout=30000)
            print("Success! Reached the paystubs page.")
            page.pause() # Final pause to verify
        except Exception as e:
            print(f"Failed to reach final ELS page: {e}")
            page.pause()

        context.close()

if __name__ == "__main__":
    run_debug()
