import sqlite3
from datetime import datetime
from bs4 import BeautifulSoup
import os
import traceback

# --- Configuration ---
# Use a separate DB for debugging so we don't corrupt the real one
DB_NAME = 'payroll_audit_debug.db'

def setup_database():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS paystubs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, pay_date TEXT UNIQUE, period_ending TEXT,
        net_pay REAL, gross_pay REAL, total_deductions REAL, agency TEXT,
        remarks TEXT, file_source TEXT, pay_period_num INTEGER
    )''')
    try: c.execute("ALTER TABLE paystubs ADD COLUMN pay_period_num INTEGER")
    except sqlite3.OperationalError: pass

    c.execute('''CREATE TABLE IF NOT EXISTS earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, paystub_id INTEGER, type TEXT,
        rate REAL, hours_current REAL, hours_adjusted REAL, amount_current REAL,
        amount_adjusted REAL, amount_ytd REAL, FOREIGN KEY(paystub_id) REFERENCES paystubs(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS deductions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, paystub_id INTEGER, type TEXT,
        amount_current REAL, amount_adjusted REAL, amount_ytd REAL,
        FOREIGN KEY(paystub_id) REFERENCES paystubs(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS leave_balances (
        id INTEGER PRIMARY KEY AUTOINCREMENT, paystub_id INTEGER, type TEXT,
        balance_start REAL, earned_current REAL, used_current REAL, balance_end REAL,
        FOREIGN KEY(paystub_id) REFERENCES paystubs(id)
    )''')

    conn.commit()
    return conn

def clean_float(text):
    if not text or text.strip() == "": return 0.0
    try: return float(text.replace(',', '').replace('$', '').strip())
    except ValueError: return 0.0

def convert_date(date_str):
    if not date_str: return None
    try: return datetime.strptime(date_str.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError: return date_str

def parse_html_paystub(html_content, filename, conn):
    print(f"\n{'='*50}\nDEBUGGING FILE: {filename}\n{'='*50}")
    soup = BeautifulSoup(html_content, 'html.parser')
    c = conn.cursor()

    try:
        print("[DEBUG] Extracting Metadata...")
        raw_date = soup.find(id="lblPayPeriodDate").get_text().strip()
        pay_date = convert_date(raw_date)
        print(f"  -> Pay Date: {pay_date}")

        pp_node = soup.find(id="lblPayPeriodNumber")
        pay_period_num = int(pp_node.get_text().strip()) if pp_node else None
        print(f"  -> Pay Period: {pay_period_num}")

        c.execute("SELECT id FROM paystubs WHERE pay_date = ?", (pay_date,))
        if c.fetchone():
            print(f"  -> File already exists in DB. Skipping insert to force full parse test.")

        period_ending = convert_date(soup.find(id="lblPayPeriodEndingDate").get_text().strip())
        net_pay = clean_float(soup.find(id="lblNetPayCurrent").get_text())
        agency = soup.find(id="lblAgencyName").get_text().strip()
        
        print("[DEBUG] Extracting Pay Table...")
        pay_table = soup.find("table", {"id": "Pay"})
        if pay_table:
            rows = pay_table.find_all("tr")
            print(f"  -> Found 'Pay' table with {len(rows)} rows.")
            for i, r in enumerate(rows):
                cols = r.find_all("td")
                print(f"    Row {i}: {len(cols)} columns")
            
            gross_pay = clean_float(rows[1].find_all("td")[1].get_text())
            total_deducs = clean_float(rows[2].find_all("td")[1].get_text())
            print(f"  -> Gross Pay: {gross_pay}, Total Deductions: {total_deducs}")
        else:
            print("  -> ERROR: 'Pay' table not found!")

        remarks_node = soup.find(id="lblRemarks")
        remarks = remarks_node.get_text("\n").strip() if remarks_node else ""

        # Use a dummy ID for testing insertion logic without breaking constraints
        paystub_id = 999 

    except Exception as e:
        print(f"\n[CRASH IN METADATA/PAY SECTION]")
        traceback.print_exc()
        return

    try:
        print("\n[DEBUG] Extracting Earnings...")
        earnings_table = soup.find("table", {"id": "Earnings"})
        if earnings_table:
            rows = earnings_table.find_all("tr")
            print(f"  -> Found 'Earnings' table with {len(rows)} rows.")
            for i, row in enumerate(rows[1:], start=1):
                cols = row.find_all("td")
                print(f"    Row {i}: {len(cols)} columns")
                if len(cols) >= 6:
                    # Intentionally doing exactly what ingest.py does to catch the error
                    t = cols[0].get_text().strip()
                    rt = clean_float(cols[1].get_text())
                    aa = clean_float(cols[2].get_text())
                    ha = clean_float(cols[3].get_text())
                    hc = clean_float(cols[4].get_text())
                    ac = clean_float(cols[5].get_text())
                    ay = clean_float(cols[6].get_text())
        else:
            print("  -> No 'Earnings' table found.")
    except Exception as e:
        print(f"\n[CRASH IN EARNINGS SECTION]")
        traceback.print_exc()
        return

    try:
        print("\n[DEBUG] Extracting Deductions...")
        for table_id in ["Deduction0", "Deduction1"]:
            ded_table = soup.find("table", {"id": table_id})
            if ded_table:
                rows = ded_table.find_all("tr")
                print(f"  -> Found '{table_id}' table with {len(rows)} rows.")
                for i, row in enumerate(rows[1:], start=1):
                    cols = row.find_all("td")
                    print(f"    Row {i}: {len(cols)} columns")
                    if len(cols) >= 5:
                        t = cols[0].get_text().strip()
                        aa = clean_float(cols[2].get_text())
                        ac = clean_float(cols[3].get_text())
                        ay = clean_float(cols[4].get_text())
    except Exception as e:
        print(f"\n[CRASH IN DEDUCTIONS SECTION]")
        traceback.print_exc()
        return

    try:
        print("\n[DEBUG] Extracting Leave...")
        leave_table = soup.find("table", {"id": "Leave"})
        if leave_table:
            rows = leave_table.find_all("tr")
            print(f"  -> Found 'Leave' table with {len(rows)} rows.")
            for i, row in enumerate(rows[1:], start=1):
                cols = row.find_all("td")
                print(f"    Row {i}: {len(cols)} columns")
                if len(cols) >= 9:
                    t = cols[0].get_text().strip()
                    bs = clean_float(cols[1].get_text())
                    ec = clean_float(cols[3].get_text())
                    uc = clean_float(cols[5].get_text())
                    be = clean_float(cols[8].get_text())
    except Exception as e:
        print(f"\n[CRASH IN LEAVE SECTION]")
        traceback.print_exc()
        return

    print("\n[SUCCESS] File parsed without crashing.")

if __name__ == "__main__":
    db_conn = setup_database()
    paystubs_dir = os.path.join(os.getcwd(), "PayStubs")

    if not os.path.exists(paystubs_dir):
        print(f"Error: Directory '{paystubs_dir}' not found.")
    else:
        # Just test the two failing files to keep output clean, plus one known good one for comparison
        target_files = ["2026-08-08.html", "2026-08-22.html", "2026-09-05.html"]
        files = [f for f in sorted(os.listdir(paystubs_dir)) if f in target_files]
        
        for filename in files:
            file_path = os.path.join(paystubs_dir, filename)
            with open(file_path, "r", encoding='utf-8') as f:
                parse_html_paystub(f.read(), filename, db_conn)
                
    db_conn.close()
