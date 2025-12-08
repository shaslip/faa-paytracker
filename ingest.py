import sqlite3
from bs4 import BeautifulSoup
import os
from datetime import datetime

DB_NAME = 'payroll_audit.db'

def convert_date(date_str):
    if not date_str: return None
    try:
        return datetime.strptime(date_str.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return date_str

def clean_float(text):
    if not text: return 0.0
    try:
        clean = text.replace(',', '').replace('$', '').strip()
        return float(clean)
    except ValueError:
        return 0.0

def run():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 1. SETUP SCHEMA
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
    c.execute('''CREATE TABLE IF NOT EXISTS earnings (id INTEGER PRIMARY KEY, paystub_id INTEGER, type TEXT, rate REAL, hours_current REAL, hours_adjusted REAL, amount_current REAL, amount_adjusted REAL, amount_ytd REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS deductions (id INTEGER PRIMARY KEY, paystub_id INTEGER, type TEXT, amount_current REAL, amount_adjusted REAL, amount_ytd REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS leave_balances (id INTEGER PRIMARY KEY, paystub_id INTEGER, type TEXT, balance_start REAL, earned_current REAL, used_current REAL, balance_end REAL)''')

    # 2. PROCESS FILES
    folder = "PayStubs"
    if not os.path.exists(folder):
        print(f"Error: Folder '{folder}' not found.")
        return

    files = sorted([f for f in os.listdir(folder) if f.endswith(".html")])
    print(f"Found {len(files)} paystubs to process...")

    for filename in files:
        path = os.path.join(folder, filename)
        with open(path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'html.parser')
            
        try:
            # Meta
            p_date_raw = soup.find(id="lblPayPeriodDate").get_text()
            p_date = convert_date(p_date_raw)
            
            # Check Exist
            if c.execute("SELECT id FROM paystubs WHERE pay_date = ?", (p_date,)).fetchone():
                print(f"Skipping {p_date} (Exists)")
                continue

            p_end = convert_date(soup.find(id="lblPayPeriodEndingDate").get_text())
            net = clean_float(soup.find(id="lblNetPayCurrent").get_text())
            agency = soup.find(id="lblAgencyName").get_text().strip()
            
            # Gross/Ded/Remarks
            pay_tbl = soup.find("table", {"id": "Pay"}).find_all("tr")
            gross = clean_float(pay_tbl[1].find_all("td")[1].get_text())
            deducs = clean_float(pay_tbl[2].find_all("td")[1].get_text())
            
            # CRITICAL: EXTRACT REMARKS
            rem_node = soup.find(id="lblRemarks")
            remarks_txt = rem_node.get_text("\n").strip() if rem_node else ""

            print(f"Inserting {p_date}... Remarks Length: {len(remarks_txt)}")

            # INSERT PAYSTUB (This is the specific line your old script was likely missing 'remarks' in)
            c.execute('''INSERT INTO paystubs (pay_date, period_ending, net_pay, gross_pay, total_deductions, agency, remarks, file_source) 
                         VALUES (?,?,?,?,?,?,?,?)''', 
                      (p_date, p_end, net, gross, deducs, agency, remarks_txt, filename))
            stub_id = c.lastrowid

            # INSERT EARNINGS
            et = soup.find("table", {"id": "Earnings"})
            if et:
                for row in et.find_all("tr")[1:]:
                    cols = row.find_all("td")
                    c.execute("INSERT INTO earnings (paystub_id, type, rate, amount_adjusted, hours_adjusted, hours_current, amount_current, amount_ytd) VALUES (?,?,?,?,?,?,?,?,?)",
                              (stub_id, cols[0].text.strip(), clean_float(cols[1].text), clean_float(cols[2].text), clean_float(cols[3].text), clean_float(cols[4].text), clean_float(cols[5].text), clean_float(cols[6].text), 0.0))

            # INSERT DEDUCTIONS
            for tid in ["Deduction0", "Deduction1"]:
                dt = soup.find("table", {"id": tid})
                if dt:
                    for row in dt.find_all("tr")[1:]:
                        cols = row.find_all("td")
                        c.execute("INSERT INTO deductions (paystub_id, type, amount_adjusted, amount_current, amount_ytd) VALUES (?,?,?,?,?)",
                                  (stub_id, cols[0].text.strip(), clean_float(cols[2].text), clean_float(cols[3].text), clean_float(cols[4].text)))

            # INSERT LEAVE
            lt = soup.find("table", {"id": "Leave"})
            if lt:
                for row in lt.find_all("tr")[1:]:
                    cols = row.find_all("td")
                    c.execute("INSERT INTO leave_balances (paystub_id, type, balance_start, earned_current, used_current, balance_end) VALUES (?,?,?,?,?,?)",
                              (stub_id, cols[0].text.strip(), clean_float(cols[1].text), clean_float(cols[3].text), clean_float(cols[5].text), clean_float(cols[8].text)))
            
            conn.commit()

        except Exception as e:
            print(f"Error on {filename}: {e}")

    conn.close()

if __name__ == "__main__":
    run()
