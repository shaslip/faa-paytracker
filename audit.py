import sqlite3
import pandas as pd

DB_NAME = 'payroll_audit.db'

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  # Access columns by name
    return conn

def get_latest_paystub_id():
    """Finds the ID of the most recent paystub by pay_date."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, pay_date, gross_pay FROM paystubs ORDER BY pay_date DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if row:
        return row['id'], row['pay_date'], row['gross_pay']
    return None, None, None

def analyze_paystub(stub_id):
    """
    Extracts the 'Rules' from a specific paystub.
    Returns a dictionary of rates and fixed costs.
    """
    conn = get_db_connection()

    # 1. Get Earnings Rates (Hourly, etc)
    earnings = pd.read_sql(f"SELECT type, rate FROM earnings WHERE paystub_id = {stub_id}", conn)

    # 2. Get Deductions (Categorize as Fixed vs Variable)
    deductions = pd.read_sql(f"SELECT type, amount_current FROM deductions WHERE paystub_id = {stub_id}", conn)

    conn.close()

    return earnings, deductions

def compare_stubs(old_stub_id, new_stub_id):
    """
    The Anomaly Detector.
    Checks if the NEW stub has codes that the OLD stub didn't have.
    """
    conn = get_db_connection()

    # Get sets of codes (types)
    old_earn = set(pd.read_sql(f"SELECT type FROM earnings WHERE paystub_id = {old_stub_id}", conn)['type'])
    new_earn = set(pd.read_sql(f"SELECT type FROM earnings WHERE paystub_id = {new_stub_id}", conn)['type'])

    old_ded = set(pd.read_sql(f"SELECT type FROM deductions WHERE paystub_id = {old_stub_id}", conn)['type'])
    new_ded = set(pd.read_sql(f"SELECT type FROM deductions WHERE paystub_id = {new_stub_id}", conn)['type'])

    conn.close()

    errors = []

    # Check for NEW Earnings codes
    new_earnings_codes = new_earn - old_earn
    if new_earnings_codes:
        errors.append(f"ALERT: New Earning Code detected: {new_earnings_codes}")

    # Check for NEW Deduction codes (Dangerous!)
    new_deduction_codes = new_ded - old_ded
    if new_deduction_codes:
        errors.append(f"CRITICAL: New Deduction appearing: {new_deduction_codes}")

    # Check for MISSING Deductions (Did health insurance drop off?)
    missing_deductions = old_ded - new_ded
    # Filter out variable things like Taxes which might legitimately be 0 if gross is 0
    # But usually, keeping this strict is better.
    if missing_deductions:
        errors.append(f"WARNING: Deduction disappeared: {missing_deductions}")

    return errors

def calculate_effective_tax_rate(stub_id, gross_pay):
    """Calculates the % of Gross that went to Federal/State/FICA."""
    conn = get_db_connection()

    # Sum up all tax-related deductions
    # Note: You might need to adjust the WHERE clause if your tax names vary
    sql = f"""
        SELECT SUM(amount_current)
        FROM deductions
        WHERE paystub_id = {stub_id}
        AND (type LIKE '%Tax%' OR type LIKE '%OASDI%' OR type LIKE '%Medicare%')
    """
    cur = conn.cursor()
    cur.execute(sql)
    total_tax = cur.fetchone()[0] or 0.0
    conn.close()

    if gross_pay > 0:
        return (total_tax / gross_pay) * 100
    return 0.0

def run_historical_audit():
    """Loops through ALL paystubs in chronological order and compares them."""
    conn = get_db_connection()
    # Fetch all stubs ordered by date (Oldest first)
    stubs = pd.read_sql("SELECT id, pay_date, net_pay FROM paystubs ORDER BY pay_date ASC", conn)
    conn.close()

    print(f"\n=== HISTORICAL AUDIT ({len(stubs)} files) ===")

    # Loop through the list
    for i in range(1, len(stubs)):
        current_stub = stubs.iloc[i]
        prev_stub = stubs.iloc[i-1]

        print(f"Checking {current_stub['pay_date']}...", end=" ")

        # Run the comparison logic we already wrote
        errors = compare_stubs(prev_stub['id'], current_stub['id'])

        if not errors:
            print("OK")
        else:
            print("FLAGS DETECTED:")
            for e in errors:
                print(f"  - {e}")

# --- Execution ---
if __name__ == "__main__":
    # 1. Run the History Audit first
    run_historical_audit()

    # 2. Show the latest snapshot (existing logic)
    latest_id, latest_date, latest_gross = get_latest_paystub_id()
    if latest_id:
        print(f"\n--- Current Baseline: {latest_date} ---")
        eff_rate = calculate_effective_tax_rate(latest_id, latest_gross)
        print(f"Effective Tax Rate: {eff_rate:.2f}%")
