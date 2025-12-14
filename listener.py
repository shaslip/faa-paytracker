import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import pandas as pd
from datetime import datetime, timedelta  # Added timedelta

# --- CONFIGURATION ---
DB_NAME = 'payroll_audit.db' 
HOST = "0.0.0.0"             
PORT = 5000

# REFERENCE DATE: A known Pay Period End date (e.g., Dec 14, 2024)
# Used to calculate future cycles mathematically.
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
            # Calculates days since reference to find the next 14-day cycle end
            delta_days = (dt_obj - REF_DATE).days
            
            # Logic: (Delta // 14) + 1 gives the next cycle index
            # If date matches REF_DATE exactly, delta is 0, index is 1 (next period end is +14 days)
            # Actually, if date is ON the PPE, it belongs to that PPE. 
            # We need to ensure the period_ending date is >= day_date.
            
            # We assume REF_DATE is a valid Period Ending.
            # If dt_obj is REF_DATE, period_ending is REF_DATE.
            # If dt_obj is REF_DATE + 1, period_ending is REF_DATE + 14.
            
            days_into_cycle = delta_days % 14
            # If days_into_cycle is 0 (it matches a PPE), offset is 0. 
            # If it's 1 day past, we need 13 days to reach next PPE.
            
            days_to_add = (14 - days_into_cycle) if days_into_cycle != 0 else 0
            
            # Correction: Wait, if delta is negative (past), this math holds?
            # Simpler approach: find the ceiling multiple of 14 relative to reference
            
            # If dt_obj > REF_DATE:
            # target = REF_DATE + ceil((dt - ref) / 14) * 14 ??
            
            # Let's use the simplest logic:
            # We want the nearest date >= dt_obj that matches (REF_DATE + N*14)
            
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
    Returns the standard schedule from the 'user_schedule' table.
    """
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        # Query the exact table used by your Dashboard
        rows = c.execute("SELECT day_of_week, start_time, end_time FROM user_schedule ORDER BY day_of_week").fetchall()
        
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
