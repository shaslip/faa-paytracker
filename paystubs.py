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
OUTPUT_DIR = "paystubs"

def login(page):
    print("Navigating to Employee Express (will redirect to login.gov)...")
    page.goto(START_URL)

    # Note: Depending on session state, you might land on an Employee Express 
    # login portal first. If there's a specific "Login.gov" button, click it.
    if page.locator('text="Sign in with Login.gov"').is_visible():
        page.click('text="Sign in with Login.gov"')

    print("Waiting for Login.gov email field...")
    page.wait_for_selector('input[type="email"]', timeout=15000)
    
    page.fill('input[type="email"]', EMAIL)
    page.fill('input[type="password"]', PASSWORD)
    page.click('button:has-text("Sign in"), button[type="submit"]')
    
    print("Credentials submitted. Waiting for 2FA screen...")
    
    # Wait for the TOTP input field
    # Login.gov usually uses name="code" for the authenticator input
    page.wait_for_selector('input[name="code"], input[id="code"]', timeout=15000)
    
    print("Generating TOTP code...")
    totp = pyotp.TOTP(TOTP_SECRET)
    current_code = totp.now()
    
    page.fill('input[name="code"], input[id="code"]', current_code)
    page.click('button:has-text("Submit"), button[type="submit"]')
    
    print("2FA submitted. Waiting to return to Employee Express...")
    
    # Wait for the Earnings and Leave Statement dropdown to prove we made it
    page.wait_for_selector('#ddlELS', timeout=30000)
    print("Successfully reached the Paystubs (ELS) page.")

def run():
    # Ensure output directory exists
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    with sync_playwright() as p:
        # headless=False is helpful for the first run to ensure login.gov selectors haven't changed
        browser = p.chromium.launch(headless=True) 
        context = browser.new_context()
        page = context.new_page()

        login(page)
        
        print("Finding available pay periods...")
        
        # Extract all options from the dropdown to a list of dictionaries
        # We do this first because selecting an option reloads the page, which would make elements stale
        options = page.locator('#ddlELS option')
        pay_periods = []
        for i in range(options.count()):
            pay_periods.append({
                "value": options.nth(i).get_attribute("value"),
                "text": options.nth(i).inner_text()
            })
            
        print(f"Found {len(pay_periods)} pay periods.")

        for pp in pay_periods:
            # Extract date from text (e.g., "08/22/2026 - Department of Transportation")
            date_match = re.search(r'(\d{2}/\d{2}/\d{4})', pp['text'])
            if not date_match:
                continue
                
            # Format date for a clean filename: YYYY-MM-DD
            raw_date = date_match.group(1)
            m, d, y = raw_date.split('/')
            formatted_date = f"{y}-{m}-{d}"
            
            filename = os.path.join(OUTPUT_DIR, f"paystub_{formatted_date}.html")
            
            # Skip if we already downloaded it
            if os.path.exists(filename):
                print(f"Skipping {formatted_date} (Already exists)")
                continue

            print(f"Downloading paystub for {formatted_date}...")
            
            # Select the option. ASP.NET uses __doPostBack which triggers a page reload.
            try:
                with page.expect_navigation(timeout=10000):
                    page.select_option('#ddlELS', pp['value'])
            except Exception:
                # If it's an AJAX update instead of a full reload, expect_navigation will timeout.
                # We catch the timeout and wait for network idle instead.
                page.wait_for_load_state('networkidle')
                time.sleep(1) # Brief pause to ensure DOM is fully rendered

            # Save the entire HTML of the page
            page_html = page.content()
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(page_html)
                
            print(f"Saved: {filename}")

        print("All paystubs downloaded successfully.")
        browser.close()

if __name__ == "__main__":
    run()
