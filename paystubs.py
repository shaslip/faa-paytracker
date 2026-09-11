import os
import time
import re
import pyotp
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Load environment variables
load_dotenv()
EMAIL = os.getenv("LOGIN_GOV_EMAIL")
PASSWORD = os.getenv("LOGIN_GOV_PASSWORD")
TOTP_SECRET = os.getenv("LOGIN_GOV_TOTP_SECRET")

# Configuration
START_URL = "https://www.employeeexpress.gov/ELS"
OUTPUT_DIR = "PayStubs"
PROFILE_DIR = "./playwright_profile"

def login(page):
    print("Navigating to Employee Express...")
    page.goto(START_URL)
    time.sleep(2)

    # 1. Click Login.gov Button
    login_btn = page.locator('a[title="Sign in with Login.gov"]')
    if login_btn.is_visible():
        print("Found Login.gov button. Clicking...")
        login_btn.click()
        time.sleep(3)

    # 2. Enter Credentials
    if page.locator('input[type="email"]').is_visible():
        print("Entering email and password...")
        page.fill('input[type="email"]', EMAIL)
        page.fill('input[type="password"]', PASSWORD)
        time.sleep(1)
        page.click('button:has-text("Sign in"), button[type="submit"]')
        time.sleep(3)

    # 3. Enter 2FA
    totp_input = page.locator('input[autocomplete="one-time-code"]')
    if totp_input.is_visible():
        print("Generating and submitting TOTP code...")
        totp = pyotp.TOTP(TOTP_SECRET)
        totp_input.fill(totp.now())
        time.sleep(1)
        page.click('button:has-text("Submit"), button[type="submit"]')
        
        # Wait for the post-login redirect to settle
        page.wait_for_load_state('networkidle')
        time.sleep(3)

    # 4. Ensure we are actually on the ELS page
    if "/Home" in page.url or "/ELS" not in page.url:
        print("Redirected to Home page. Navigating to Paystubs (ELS)...")
        page.goto(START_URL)
        time.sleep(2)

    # 5. Verify Success
    page.wait_for_selector('#ddlELS', timeout=30000)
    print("Successfully reached the Paystubs page.")

def run():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    with sync_playwright() as p:
        print("Launching Playwright...")
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=True, # Runs invisibly in the background
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox"
            ],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        
        page = context.pages[0] if context.pages else context.new_page()

        try:
            login(page)
            
            print("Finding available pay periods...")
            options = page.locator('#ddlELS option')
            pay_periods = []
            
            # Extract dropdown options
            for i in range(options.count()):
                pay_periods.append({
                    "value": options.nth(i).get_attribute("value"),
                    "text": options.nth(i).inner_text()
                })
                
            print(f"Found {len(pay_periods)} pay periods.")

            for pp in pay_periods:
                # Extract date: e.g., "08/22/2026 - Department of Transportation"
                date_match = re.search(r'(\d{2}/\d{2}/\d{4})', pp['text'])
                if not date_match:
                    continue
                    
                raw_date = date_match.group(1)
                m, d, y = raw_date.split('/')
                formatted_date = f"{y}-{m}-{d}"
                
                filename = os.path.join(OUTPUT_DIR, f"{formatted_date}.html")
                
                # Skip if we already have it
                if os.path.exists(filename):
                    print(f"Skipping {formatted_date} (Already exists)")
                    continue

                print(f"Downloading paystub for {formatted_date}...")
                
                # Select the dropdown option
                page.select_option('#ddlELS', pp['value'])
                
                # If this is the very first item (default selection), give the initial page load time to settle
                if pp == pay_periods[0]:
                    time.sleep(6)
                
                # Wait explicitly for the AJAX request to finish by checking the date label
                try:
                    page.locator(f"#lblPayPeriodEndingDate:has-text('{raw_date}')").wait_for(state="visible", timeout=15000)
                    time.sleep(1) # Extra second buffer for the rest of the DOM to settle
                except Exception:
                    print(f"  -> Warning: Timeout waiting for {raw_date} to render.")
                    time.sleep(6)

                # Save the raw HTML
                page_html = page.content()
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(page_html)
                    
                print(f"Saved: {filename}")

            print("All missing paystubs downloaded successfully.")
            
        except Exception as e:
            print(f"An error occurred: {e}")
            
        finally:
            context.close()

if __name__ == "__main__":
    run()
