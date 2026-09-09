import os
import pyotp
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
EMAIL = os.getenv("LOGIN_GOV_EMAIL")
PASSWORD = os.getenv("LOGIN_GOV_PASSWORD")
TOTP_SECRET = os.getenv("LOGIN_GOV_TOTP_SECRET")
START_URL = "https://www.employeeexpress.gov/ELS"

def run_debug():
    with sync_playwright() as p:
        # headless=False keeps it visible, slow_mo lets us watch the interactions
        browser = p.chromium.launch(headless=False, slow_mo=500)
        context = browser.new_context()
        page = context.new_page()

        print(f"Navigating to {START_URL}...")
        page.goto(START_URL)

        print("Looking for Login.gov button...")
        login_btn = page.locator('a[title="Sign in with Login.gov"]')
        if login_btn.is_visible():
            print("Found button. Clicking...")
            login_btn.click()
        else:
            print("Login button not found! Pausing...")
            page.pause()

        print("Waiting for Login.gov email field...")
        try:
            page.wait_for_selector('input[type="email"]', timeout=10000)
            page.fill('input[type="email"]', EMAIL)
            page.fill('input[type="password"]', PASSWORD)
            page.click('button:has-text("Sign in"), button[type="submit"]')
        except Exception as e:
            print(f"Failed at credentials step: {e}")
            page.pause()

        print("Waiting for 2FA screen...")
        try:
            page.wait_for_selector('input[name="code"], input[id="code"]', timeout=15000)
            print("Generating and submitting TOTP code...")
            totp = pyotp.TOTP(TOTP_SECRET)
            page.fill('input[name="code"], input[id="code"]', totp.now())
            page.click('button:has-text("Submit"), button[type="submit"]')
        except Exception as e:
            print(f"Failed at 2FA step: {e}")
            page.pause()

        print("Waiting to return to Employee Express (ELS dropdown)...")
        try:
            page.wait_for_selector('#ddlELS', timeout=30000)
            print("Success! Reached the paystubs page.")
            page.pause() # Pause at the very end so you can confirm it worked before it closes
        except Exception as e:
            print(f"Failed to reach final ELS page: {e}")
            page.pause()

        browser.close()

if __name__ == "__main__":
    run_debug()
