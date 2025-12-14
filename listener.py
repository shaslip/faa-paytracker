import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import pandas as pd
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
async def get_schedule_defaults(year: Optional[int] = None):
    """
    Returns the standard schedule. Defaults to current year if not specified.
    """
    # FIX: Default to current year if mobile app doesn't send one
    target_year = year if year else datetime.now().year
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        # FIX: Filter by YEAR to prevent duplicate rows from different years
        rows = c.execute(
            "SELECT day_of_week, start_time, end_time FROM user_schedule WHERE year = ? ORDER BY day_of_week", 
            (target_year,)
        ).fetchall()
        
        schedule = {}
        for row in rows:
            schedule[row['day_of_week']] = {
                "start": row['start_time'], 
                "end": row['end_time']
            }
        return schedule
    except Exception as e:
        print(f"Error serving defaults: {e}")
        return {}
    finally:
        conn.close()

if __name__ == "__main__":
    print(f"🚀 Listener active at http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
