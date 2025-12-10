import sqlite3
import pandas as pd
from datetime import datetime, timedelta

DB_NAME = 'payroll_audit.db'

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def setup_database():
    """Initializes all tables including V2 timesheets and Schedule."""
    conn = get_db()
    
    # 1. Master Paystubs (Existing)
    conn.execute('''CREATE TABLE IF NOT EXISTS paystubs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, pay_date TEXT UNIQUE, period_ending TEXT,
        net_pay REAL, gross_pay REAL, total_deductions REAL, agency TEXT, remarks TEXT, file_source TEXT
    )''')
    
    # 2. Schedule (Default Template)
    conn.execute('''CREATE TABLE IF NOT EXISTS user_schedule (
        day_of_week INTEGER PRIMARY KEY, start_time TEXT, end_time TEXT, is_workday BOOLEAN
    )''')
    
    # Seed Schedule if empty
    if pd.read_sql("SELECT count(*) as c FROM user_schedule", conn).iloc[0]['c'] == 0:
        for i in range(7):
            is_work = i < 5 
            conn.execute("INSERT INTO user_schedule VALUES (?, ?, ?, ?)", 
                         (i, "07:00" if is_work else None, "15:00" if is_work else None, is_work))

    # 3. Timesheet V2 (Start/End Times)
    conn.execute('''CREATE TABLE IF NOT EXISTS timesheet_entry_v2 (
        period_ending TEXT, day_date TEXT, start_time TEXT, end_time TEXT,
        leave_type TEXT, ojti_hours REAL DEFAULT 0, cic_hours REAL DEFAULT 0,
        UNIQUE(period_ending, day_date)
    )''')
    conn.commit()
    conn.close()

def save_user_schedule(df):
    conn = get_db()
    c = conn.cursor()
    for _, row in df.iterrows():
        s = row['start_time']
        e = row['end_time']
        
        # If user cleared the cell, it might be None or empty string
        if s == "" or pd.isna(s): s = None
        if e == "" or pd.isna(e): e = None
        
        is_workday = 1 if s is not None else 0
        
        # Since input is now TextColumn, s and e are ALREADY strings ("07:00").
        # We don't need strftime.
        
        c.execute("UPDATE user_schedule SET start_time=?, end_time=?, is_workday=? WHERE day_of_week=?", 
                  (s, e, is_workday, row['day_of_week']))
    conn.commit()
    conn.close()
    
def get_paystubs_meta():
    conn = get_db()
    df = pd.read_sql("SELECT id, pay_date, period_ending, net_pay, gross_pay, file_source FROM paystubs ORDER BY pay_date DESC", conn)
    conn.close()
    return df

def get_full_paystub_data(stub_id):
    conn = get_db()
    stub = dict(conn.execute("SELECT * FROM paystubs WHERE id = ?", (stub_id,)).fetchone())
    earnings = pd.read_sql("SELECT * FROM earnings WHERE paystub_id = ?", conn, params=(stub_id,))
    deductions = pd.read_sql("SELECT * FROM deductions WHERE paystub_id = ?", conn, params=(stub_id,))
    leave = pd.read_sql("SELECT * FROM leave_balances WHERE paystub_id = ?", conn, params=(stub_id,))
    conn.close()
    return {'stub': stub, 'earnings': earnings, 'deductions': deductions, 'leave': leave}

def get_pay_period_dates(period_ending_str):
    end_date = datetime.strptime(period_ending_str, "%Y-%m-%d")
    dates = []
    start_date = end_date - timedelta(days=13)
    for i in range(14):
        d = start_date + timedelta(days=i)
        dates.append(d.strftime("%Y-%m-%d"))
    return dates

def load_timesheet_v2(period_ending):
    conn = get_db()
    defaults = pd.read_sql("SELECT * FROM user_schedule", conn).set_index('day_of_week')
    saved = pd.read_sql("SELECT * FROM timesheet_entry_v2 WHERE period_ending = ?", conn, params=(period_ending,))
    conn.close()
    
    dates = get_pay_period_dates(period_ending)
    data = []
    
    for d in dates:
        dt = datetime.strptime(d, "%Y-%m-%d")
        day_idx = dt.weekday()
        
        row = saved[saved['day_date'] == d] if not saved.empty else pd.DataFrame()
        
        if not row.empty:
            # RETURN STRINGS directly from DB
            r = row.iloc[0]
            data.append({
                "Date": d,
                "Start": r['start_time'], # String "07:00" or None
                "End": r['end_time'],     # String "15:00" or None
                "Leave_Type": r['leave_type'],
                "OJTI": r['ojti_hours'],
                "CIC": r['cic_hours']
            })
        else:
            def_row = defaults.loc[day_idx] if day_idx in defaults.index else None
            if def_row is not None and def_row['is_workday']:
                # RETURN STRINGS directly from defaults
                data.append({
                    "Date": d, 
                    "Start": def_row['start_time'], 
                    "End": def_row['end_time'], 
                    "Leave_Type": None,
                    "OJTI": 0.0, 
                    "CIC": 0.0
                })
            else:
                data.append({
                    "Date": d, "Start": None, "End": None, 
                    "Leave_Type": None, "OJTI": 0.0, "CIC": 0.0
                })
                
    return pd.DataFrame(data)

def save_timesheet_v2(period_ending, df):
    conn = get_db()
    c = conn.cursor()
    for _, row in df.iterrows():
        s_str = row['Start']
        e_str = row['End']
        
        # Clean up empty strings -> None for DB
        if not s_str: s_str = None
        if not e_str: e_str = None
        
        # --- FIX: Sanitize Leave_Type ---
        l_type = row['Leave_Type']
        if isinstance(l_type, list): 
            l_type = l_type[0] if l_type else None
        if pd.isna(l_type) or l_type == "": 
            l_type = None
        # --------------------------------
        
        c.execute("""
            INSERT INTO timesheet_entry_v2 (period_ending, day_date, start_time, end_time, leave_type, ojti_hours, cic_hours)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(period_ending, day_date) DO UPDATE SET
            start_time=excluded.start_time, end_time=excluded.end_time,
            leave_type=excluded.leave_type, ojti_hours=excluded.ojti_hours, cic_hours=excluded.cic_hours
        """, (period_ending, row['Date'], s_str, e_str, l_type, row['OJTI'], row['CIC']))
    conn.commit()
    conn.close()

def get_reference_data(current_stub_id):
    """Finds best available rates/deductions (History Fallback Logic for Shutdowns)."""
    conn = get_db()
    curr_earnings = pd.read_sql("SELECT * FROM earnings WHERE paystub_id = ?", conn, params=(current_stub_id,))
    reg_rows = curr_earnings[curr_earnings['type'].str.contains('Regular', case=False, na=False)]
    
    if not reg_rows.empty and reg_rows.iloc[0]['rate'] > 0:
        base_rate = reg_rows.iloc[0]['rate']
        deductions = pd.read_sql("SELECT * FROM deductions WHERE paystub_id = ?", conn, params=(current_stub_id,))
        conn.close()
        return base_rate, deductions, curr_earnings
    
    last_good = pd.read_sql("SELECT paystub_id, rate FROM earnings WHERE type LIKE '%Regular%' AND rate > 0 ORDER BY id DESC LIMIT 1", conn)
    if not last_good.empty:
        ref_id = int(last_good.iloc[0]['paystub_id'])
        ref_rate = last_good.iloc[0]['rate']
        ref_ded = pd.read_sql("SELECT * FROM deductions WHERE paystub_id = ?", conn, params=(ref_id,))
        ref_earn = pd.read_sql("SELECT * FROM earnings WHERE paystub_id = ?", conn, params=(ref_id,))
        conn.close()
        return ref_rate, ref_ded, ref_earn

    conn.close()
    return 0.0, pd.DataFrame(), pd.DataFrame()

def has_saved_timesheet(period_ending):
    """Returns True if the user has explicitly saved a timesheet for this period."""
    conn = get_db()
    c = conn.cursor()
    # We check if ANY entries exist for this period_ending in the V2 table
    c.execute("SELECT count(*) FROM timesheet_entry_v2 WHERE period_ending = ?", (period_ending,))
    count = c.fetchone()[0]
    conn.close()
    return count > 0
