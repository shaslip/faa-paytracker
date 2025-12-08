import sqlite3
from datetime import datetime
from bs4 import BeautifulSoup
import os

# --- Configuration ---
DB_NAME = 'payroll_audit.db'

def setup_database():
    """Creates the schema with support for Adjusted amounts."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # 1. Master Paystub Record
    c.execute('''CREATE TABLE IF NOT EXISTS paystubs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pay_date TEXT UNIQUE,
        period_ending TEXT,
        net_pay REAL,
        gross_pay REAL,
        total_deductions REAL,
        agency TEXT,
        remarks TEXT,
        file_source TEXT
    )''')

    # 2. Earnings (The "Truth" with Adjusted columns)
    c.execute('''CREATE TABLE IF NOT EXISTS earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        paystub_id INTEGER,
        type TEXT,
        rate REAL,
        hours_current REAL,
        hours_adjusted REAL,
        amount_current REAL,
        amount_adjusted REAL,
        amount_ytd REAL,
        FOREIGN KEY(paystub_id) REFERENCES paystubs(id)
    )''')

    # 3. Deductions (Taxes & Benefits)
    c.execute('''CREATE TABLE IF NOT EXISTS deductions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        paystub_id INTEGER,
        type TEXT,
        amount_current REAL,
        amount_adjusted REAL,
        amount_ytd REAL,
        FOREIGN KEY(paystub_id) REFERENCES paystubs(id)
    )''')

    # 4. Leave Balances
    c.execute('''CREATE TABLE IF NOT EXISTS leave_balances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        paystub_id INTEGER,
        type TEXT,
        balance_start REAL,
        earned_current REAL,
        used_current REAL,
        balance_end REAL,
        FOREIGN KEY(paystub_id) REFERENCES paystubs(id)
    )''')

    conn.commit()
    return conn

def clean_float(text):
    """Converts '$1,234.56', empty strings, or text to float."""
    if not text or text.strip() == "":
        return 0.0
    try:
        clean = text.replace(',', '').replace('$', '').strip()
        return float(clean)
    except ValueError:
        return 0.0

def convert_date(date_str):
    """Converts MM/DD/YYYY to YYYY-MM-DD for proper SQL sorting."""
    if not date_str: return None
    try:
        return datetime.strptime(date_str.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return date_str # Fallback if format is weird

def parse_html_paystub(html_content, filename, conn):
    soup = BeautifulSoup(html_content, 'html.parser')
    c = conn.cursor()

    # --- 1. Meta Data Extraction ---
    try:
        raw_date = soup.find(id="lblPayPeriodDate").get_text().strip()
        pay_date = convert_date(raw_date)

        # Check existence with the NEW format
        c.execute("SELECT id FROM paystubs WHERE pay_date = ?", (pay_date,))
        if c.fetchone():
            print(f"Skipping {filename}: {pay_date} exists.")
            return

        raw_period = soup.find(id="lblPayPeriodEndingDate").get_text().strip()
        period_ending = convert_date(raw_period)
        net_pay = clean_float(soup.find(id="lblNetPayCurrent").get_text())
        agency = soup.find(id="lblAgencyName").get_text().strip()

        pay_table = soup.find("table", {"id": "Pay"})
        rows = pay_table.find_all("tr")
        gross_pay = clean_float(rows[1].find_all("td")[1].get_text())
        total_deducs = clean_float(rows[2].find_all("td")[1].get_text())
        
        # --- NEW: Extract Remarks ---
        remarks_node = soup.find(id="lblRemarks")
        # Get text, strip whitespace, and preserve line breaks if possible
        if remarks_node:
            print(f"DEBUG: Found Node for {pay_date}. Content: {remarks_node.get_text()[:20]}...")
        else:
            print(f"DEBUG: Node 'lblRemarks' NOT FOUND for {pay_date}")
        remarks = remarks_node.get_text("\n").strip() if remarks_node else ""

        print(f"Importing {filename}: {pay_date} (Net: ${net_pay})")

        # Insert Master Record
        c.execute('''INSERT INTO paystubs
                     (pay_date, period_ending, net_pay, gross_pay, total_deductions, agency, file_source)
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (pay_date, period_ending, net_pay, gross_pay, total_deducs, agency, filename))

        paystub_id = c.lastrowid # Get the ID of the new row

    except AttributeError as e:
        print(f"Error parsing meta data in {filename}: {e}")
        return

    # --- 2. Earnings Extraction ---
    earnings_table = soup.find("table", {"id": "Earnings"})
    if earnings_table:
        for row in earnings_table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) >= 6:
                c.execute('''INSERT INTO earnings (paystub_id, type, rate, amount_adjusted, hours_adjusted, hours_current, amount_current, amount_ytd)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                          (paystub_id,
                           cols[0].get_text().strip(),
                           clean_float(cols[1].get_text()),
                           clean_float(cols[2].get_text()),
                           clean_float(cols[3].get_text()),
                           clean_float(cols[4].get_text()),
                           clean_float(cols[5].get_text()),
                           clean_float(cols[6].get_text())
                          ))

    # --- 3. Deductions Extraction ---
    for table_id in ["Deduction0", "Deduction1"]:
        ded_table = soup.find("table", {"id": table_id})
        if ded_table:
            for row in ded_table.find_all("tr")[1:]:
                cols = row.find_all("td")
                if len(cols) >= 5:
                    c.execute('''INSERT INTO deductions (paystub_id, type, amount_adjusted, amount_current, amount_ytd)
                                 VALUES (?, ?, ?, ?, ?)''',
                              (paystub_id,
                               cols[0].get_text().strip(),
                               clean_float(cols[2].get_text()),
                               clean_float(cols[3].get_text()),
                               clean_float(cols[4].get_text())
                              ))

    # --- 4. Leave Extraction ---
    leave_table = soup.find("table", {"id": "Leave"})
    if leave_table:
        for row in leave_table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) >= 9:
                c.execute('''INSERT INTO leave_balances (paystub_id, type, balance_start, earned_current, used_current, balance_end)
                             VALUES (?, ?, ?, ?, ?, ?)''',
                          (paystub_id,
                           cols[0].get_text().strip(),
                           clean_float(cols[1].get_text()),
                           clean_float(cols[3].get_text()),
                           clean_float(cols[5].get_text()),
                           clean_float(cols[8].get_text())
                          ))

    conn.commit()

# --- Execution Block ---
if __name__ == "__main__":
    db_conn = setup_database()

    # Define the folder containing your HTML files
    paystubs_dir = os.path.join(os.getcwd(), "PayStubs")

    if not os.path.exists(paystubs_dir):
        print(f"Error: Directory '{paystubs_dir}' not found. Please create it and add your HTML files.")
    else:
        # Get all .html files and sort them (alphabetically/chronologically)
        files = sorted([f for f in os.listdir(paystubs_dir) if f.endswith(".html")])

        print(f"Found {len(files)} paystubs to process...")

        for filename in files:
            file_path = os.path.join(paystubs_dir, filename)
            try:
                with open(file_path, "r", encoding='utf-8') as f:
                    html_data = f.read()
                    parse_html_paystub(html_data, filename, db_conn)
            except Exception as e:
                print(f"Failed to process {filename}: {e}")

    db_conn.close()
