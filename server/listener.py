import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import pandas as pd
import os
import sys

# Add src to path
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(script_dir, "src"))

import logic
import models
from datetime import datetime, timedelta

# --- CONFIGURATION ---
base_dir = os.path.dirname(script_dir)
DB_NAME = os.path.join(base_dir, 'payroll_audit.db') 
HOST = "0.0.0.0"             
PORT = 5000

# REFERENCE DATE: A known Pay Period End date (e.g., Dec 14, 2024)
REF_DATE = datetime.strptime("2024-12-14", "%Y-%m-%d")

app = FastAPI()

class ShiftEntry(BaseModel):
    day_date: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    leave_type: Optional[str] = None
    ojti_hours: float = 0.0
    cic_hours: float = 0.0
    timestamp: str

@app.post("/mobile_sync")
async def ingest_mobile_data(entries: List[ShiftEntry]):
    if not entries:
        return {"status": "ignored", "message": "Empty payload"}

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Receiving {len(entries)} mobile entries...")
    
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    
    count = 0
    try:
        for entry in entries:
            dt_str = entry.day_date
            dt_obj = datetime.strptime(dt_str, "%Y-%m-%d")

            # --- Mathematical Pay Period Calculation ---
            diff = (dt_obj - REF_DATE).days
            remainder = diff % 14
            if remainder == 0:
                pe_date = dt_obj
            else:
                pe_date = dt_obj + timedelta(days=(14 - remainder))
            
            period_ending = pe_date.strftime("%Y-%m-%d")
            # -------------------------------------------

            c.execute("""
                INSERT INTO timesheet_entry_v2 
                (period_ending, day_date, start_time, end_time, leave_type, ojti_hours, cic_hours)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(period_ending, day_date) DO UPDATE SET
                start_time=excluded.start_time,
                end_time=excluded.end_time,
                leave_type=excluded.leave_type,
                ojti_hours=excluded.ojti_hours,
                cic_hours=excluded.cic_hours
            """, (
                period_ending,
                entry.day_date,
                entry.start_time,
                entry.end_time,
                entry.leave_type,
                entry.ojti_hours,
                entry.cic_hours
            ))
            count += 1
            
        conn.commit()
        print(f"Successfully saved {count} records.")
        return {"status": "success", "count": count}

    except Exception as e:
        print(f"Error processing sync: {e}")
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/get_schedule_defaults")
async def get_schedule_defaults():
    """
    Returns ALL schedule rows (all years) so the mobile app can cache them.
    """
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        # Fetch everything: Year, Day, Start, End
        rows = c.execute("SELECT year, day_of_week, start_time, end_time FROM user_schedule").fetchall()
        
        data = []
        for r in rows:
            data.append({
                "year": r['year'],
                "day": r['day_of_week'],
                "start": r['start_time'],
                "end": r['end_time']
            })
        return data
    except Exception as e:
        print(f"Error serving defaults: {e}")
        return []
    finally:
        conn.close()

@app.get("/get_saved_shifts")
async def get_saved_shifts(year: Optional[int] = None):
    """
    Returns ACTUAL saved shifts (timesheet_entry_v2) for the given year.
    This allows the mobile app to know about manual overrides (like OT on an RDO).
    """
    target_year = year if year else datetime.now().year
    
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        # Get all saved shifts for this year
        # We filter by period_ending string matching the year
        c.execute("SELECT day_date, start_time, end_time, leave_type, ojti_hours, cic_hours FROM timesheet_entry_v2 WHERE period_ending LIKE ?", (f"{target_year}%",))
        rows = c.fetchall()
        
        data = []
        for r in rows:
            data.append({
                "date": r['day_date'],
                "start": r['start_time'],
                "end": r['end_time'],
                "leave": r['leave_type'],
                "ojti": r['ojti_hours'],
                "cic": r['cic_hours']
            })
        return data
    except Exception as e:
        print(f"Error serving saved shifts: {e}")
        return []
    finally:
        conn.close()

@app.get("/get_holidays")
async def get_holidays(year: Optional[int] = None):
    """
    Calculates and returns the OBSERVED holidays for the requested year.
    Uses the server-side logic.py to ensure the 'Slide Rule' is accurate.
    """
    target_year = year if year else datetime.now().year
    
    # 1. Load the schedule for that year (needed for the slide rule)
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # Fetch schedule and format for logic.py (index by day_of_week)
        sched_df = pd.read_sql("SELECT * FROM user_schedule WHERE year = ?", conn, params=(target_year,))
    finally:
        conn.close()
    
    if sched_df.empty:
        # Fallback if no schedule exists for that year
        sched_df = pd.DataFrame([
            {'day_of_week': i, 'is_workday': 1 if i < 5 else 0} for i in range(7)
        ])
    
    # Set index for logic.get_observed_holiday
    calc_sched = sched_df.set_index('day_of_week')

    # 2. Load Raw Holidays
    all_holidays = logic.load_holidays()
    raw_dates = all_holidays.get(str(target_year), [])
    
    # Hardcoded names to match your dashboard (ensure length matches json)
    holiday_names = [
        "New Year's Day", "MLK Day", "Washington's Bday", "Memorial Day", 
        "Juneteenth", "Independence Day", "Labor Day", "Columbus Day", 
        "Veterans Day", "Thanksgiving", "Christmas"
    ]

    results = []
    # Zip safely (in case json length differs)
    for name, date_str in zip(holiday_names, raw_dates):
        actual_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        
        # 3. Apply the Slide Rule
        observed_date = logic.get_observed_holiday(actual_date, calc_sched)
        
        results.append({
            "year": target_year,
            "name": name,
            "date": observed_date.strftime("%Y-%m-%d"),
            "day": observed_date.strftime("%A")
        })
        
    return results

@app.get("/get_paystubs_summary")
async def get_paystubs_summary():
    """
    Returns high-level history (Net Pay), current leave balances, 
    and calculates the projected net pay for the next upcoming check.
    """
    stubs = models.get_paystubs_meta()
    if stubs.empty:
        return {"history": [], "leave": [], "projected": None}
        
    stubs = stubs.sort_values('period_ending', ascending=False)
    last_stub = stubs.iloc[0]
    last_stub_id = int(last_stub['id'])
    last_pe = last_stub['period_ending']
    
    # 1. Recent History (Last 5)
    history = stubs.head(5)[['period_ending', 'net_pay']].to_dict('records')
    
    # 2. Leave Balances
    full_data = models.get_full_paystub_data(last_stub_id)
    leave_df = full_data['leave']
    leave_data = []
    if not leave_df.empty:
        for _, r in leave_df.iterrows():
            leave_data.append({"type": r['type'], "balance": r['balance_end']})
            
    # 3. Projected Next Check
    last_dt = datetime.strptime(last_pe, "%Y-%m-%d")
    next_dt = last_dt + timedelta(days=14)
    next_pe = next_dt.strftime("%Y-%m-%d")
    
    ts_v2 = models.load_timesheet_v2(next_pe)
    ref_rate, ref_ded, ref_earn = models.get_reference_data(last_stub_id)
    pe_year = next_dt.year
    std_sched = models.get_user_schedule(pe_year).set_index('day_of_week')
    
    bucket_rows = []
    for _, row in ts_v2.iterrows():
        s_raw = row['Start']
        e_raw = row['End']
        s_obj = pd.to_datetime(s_raw, format='%H:%M').time() if pd.notna(s_raw) and str(s_raw).strip() not in ["None", ""] else None
        e_obj = pd.to_datetime(e_raw, format='%H:%M').time() if pd.notna(e_raw) and str(e_raw).strip() not in ["None", ""] else None
        
        b = logic.calculate_daily_breakdown(
            row['Date'], s_obj, e_obj, row['Leave_Type'], 
            float(row['OJTI']) if pd.notna(row['OJTI']) else 0.0, 
            float(row['CIC']) if pd.notna(row['CIC']) else 0.0, 
            std_sched
        )
        bucket_rows.append(b)
        
    cols = ["Regular", "Overtime", "Night", "Sunday", "Holiday", "Hol_Leave", "Leave_Hrs", "OJTI", "CIC"]
    buckets = pd.DataFrame(bucket_rows, columns=cols).fillna(0.0)
    
    dummy_meta = {
        'agency': 'FAA', 'period_ending': next_pe, 'pay_date': 'Estimated',
        'gross_pay': 0.0, 'net_pay': 0.0, 'total_deductions': 0.0,
        'remarks': 'PROJECTED ESTIMATE'
    }
    
    exp_data = logic.calculate_expected_pay(buckets, ref_rate, dummy_meta, ref_ded, pd.DataFrame(), ref_earn)
    
    projected = {
        "period_ending": next_pe,
        "net_pay": exp_data['stub']['net_pay']
    }
    
    return {"history": history, "leave": leave_data, "projected": projected, "base_rate": ref_rate}

@app.get("/get_ytd_stats")
async def get_ytd_stats(year: Optional[int] = None):
    """
    Returns aggregated YTD earnings and deductions for the requested Tax Year.
    """
    target_year = year if year else datetime.now().year
    df_earn, df_ded = models.get_all_line_items()
    
    if df_earn.empty and df_ded.empty:
        return {"earnings": [], "deductions": []}
        
    # Filter by pay_date year (Tax Year)
    df_earn['pay_date'] = pd.to_datetime(df_earn['pay_date'])
    df_earn = df_earn[df_earn['pay_date'].dt.year == target_year]
    
    df_ded['pay_date'] = pd.to_datetime(df_ded['pay_date'])
    df_ded = df_ded[df_ded['pay_date'].dt.year == target_year]
    
    def parse_hours(val):
        if isinstance(val, str) and ":" in val:
            parts = val.split(":")
            return float(parts[0]) + float(parts[1]) / 60.0
        return float(val) if pd.notna(val) else 0.0
        
    earn_data = []
    if not df_earn.empty:
        df_earn['hours_float'] = df_earn['hours_current'].apply(parse_hours)
        ytd_earn = df_earn.groupby('type').agg(Amount=('amount_current', 'sum'), Hours=('hours_float', 'sum')).reset_index()
        ytd_earn = ytd_earn.sort_values(by='Amount', ascending=False)
        earn_data = ytd_earn.to_dict('records')
        
    ded_data = []
    if not df_ded.empty:
        ytd_ded = df_ded.groupby('type').agg(Amount=('amount_current', 'sum')).reset_index()
        ytd_ded = ytd_ded.sort_values(by='Amount', ascending=False)
        ded_data = ytd_ded.to_dict('records')
        
    return {"earnings": earn_data, "deductions": ded_data}

if __name__ == "__main__":
    print(f"🚀 Listener active at http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
