import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import pandas as pd
import logic
from datetime import datetime, timedelta

# --- CONFIGURATION ---
DB_NAME = 'payroll_audit.db' 
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
    
    conn = sqlite3.connect(DB_NAME)
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
    conn = sqlite3.connect(DB_NAME)
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

@app.get("/get_holidays")
async def get_holidays(year: Optional[int] = None):
    """
    Calculates and returns the OBSERVED holidays for the requested year.
    Uses the server-side logic.py to ensure the 'Slide Rule' is accurate.
    """
    target_year = year if year else datetime.now().year
    
    # 1. Load the schedule for that year (needed for the slide rule)
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    # Fetch schedule and format for logic.py (index by day_of_week)
    sched_df = pd.read_sql("SELECT * FROM user_schedule WHERE year = ?", conn, params=(target_year,))
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

if __name__ == "__main__":
    print(f"🚀 Listener active at http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
