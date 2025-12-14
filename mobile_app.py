import flet as ft
import sqlite3
import requests
import json
import os
from datetime import datetime

# --- CONFIGURATION ---
DESKTOP_URL = "http://10.0.0.77:5000" 
if "ANDROID_ARGUMENT" in os.environ:
    # We are running on the phone
    from pathlib import Path
    # Flet/Python on Android usually has access to the internal storage via this path
    files_dir = os.environ.get("EXTERNAL_FILES_DIR", ".")
    DB_NAME = os.path.join(files_dir, "mobile_data.db")
else:
    # We are on the Desktop
    DB_NAME = "mobile_data.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS offline_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_date TEXT,
            start_time TEXT,
            end_time TEXT,
            leave_type TEXT,
            ojti_hours REAL,
            cic_hours REAL,
            timestamp TEXT
        )
    ''')
    # NEW: Table for storing the downloaded schedule
    c.execute('''
        CREATE TABLE IF NOT EXISTS schedule_defaults (
            day_idx INTEGER PRIMARY KEY,
            start_time TEXT,
            end_time TEXT
        )
    ''')
    conn.commit()
    conn.close()

def main(page: ft.Page):
    page.title = "FAA PayTracker Mobile"
    page.window_width = 400
    page.window_height = 800
    page.theme_mode = ft.ThemeMode.LIGHT
    
    init_db()

    # --- UI COMPONENTS ---
    lbl_status = ft.Text(value="Ready", color="grey")

    # 1. Date
    txt_date = ft.TextField(
        label="Date", 
        value=datetime.now().strftime("%Y-%m-%d"), 
        read_only=True,
        icon=ft.Icons.CALENDAR_TODAY,
        expand=True
    )

    def auto_colon(e):
        # We store the previous length in .data to detect backspacing
        prev_len = e.control.data if e.control.data is not None else 0
        val = e.control.value
        
        # Only add colon if user is typing FORWARD (length increased)
        if len(val) == 2 and len(val) > prev_len:
            # Ensure they typed numbers
            if val.isdigit():
                e.control.value = val + ":"
                e.control.update()
        
        # Update current length for the next keystroke check
        e.control.data = len(e.control.value)

    def change_date(e):
        # 1. Update text
        new_date = date_picker.value
        txt_date.value = new_date.strftime("%Y-%m-%d")
        
        # 2. NEW: Auto-Fill Logic
        day_idx = new_date.weekday() # 0=Mon
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        row = c.execute("SELECT start_time, end_time FROM schedule_defaults WHERE day_idx=?", (day_idx,)).fetchone()
        conn.close()
        
        if row:
            txt_start.value = row[0] if row[0] else ""
            txt_end.value = row[1] if row[1] else ""
            lbl_status.value = "Hours auto-filled."
            lbl_status.color = "blue"
        else:
            txt_start.value = ""
            txt_end.value = ""

        page.update()

    date_picker = ft.DatePicker(
        on_change=change_date,
        first_date=datetime(2023, 1, 1),
        last_date=datetime(2030, 12, 31),
    )
    page.overlay.append(date_picker)

    def open_picker(e):
        date_picker.open = True
        page.update()

    btn_pick_date = ft.IconButton(
        icon=ft.Icons.CALENDAR_MONTH,
        on_click=open_picker
    )

    # 2. Shift Times
    txt_start = ft.TextField(
        label="Start (HH:MM)", 
        hint_text="14:30", 
        width=160, 
        on_change=auto_colon
    )
    txt_end = ft.TextField(
        label="End (HH:MM)", 
        hint_text="22:30", 
        width=160, 
        on_change=auto_colon
    )

    # 3. Leave Type
    dd_leave = ft.Dropdown(
        label="Leave Type (Optional)",
        options=[
            ft.dropdown.Option("None"),
            ft.dropdown.Option("Annual"),
            ft.dropdown.Option("Sick"),
            ft.dropdown.Option("Holiday"),
            ft.dropdown.Option("Credit"),
            ft.dropdown.Option("Comp"),
            ft.dropdown.Option("LWOP"),
        ],
        value="None"
    )

    # 4. Differentials - REVERTED to ft.KeyboardType.NUMBER
    txt_ojti = ft.TextField(
        label="OJTI (HH:MM)", 
        value="",
        width=160, 
        on_change=auto_colon
    )
    txt_cic = ft.TextField(
        label="CIC (HH:MM)", 
        value="", 
        width=160, 
        on_change=auto_colon
    )

    # --- ACTIONS ---

    def save_local_click(e):
        try:
            # Helper: Converts "1:30" or "1.5" to decimal hours (1.5)
            def parse_time(val):
                val = val.strip()
                if not val: return 0.0
                if ":" in val:
                    parts = val.split(":")
                    if len(parts) != 2: raise ValueError(f"Invalid format: {val}")
                    return float(parts[0]) + (float(parts[1]) / 60.0)
                return float(val)

            ojti = parse_time(txt_ojti.value)
            cic = parse_time(txt_cic.value)
            
            # REVERTED: Original logic
            leave_val = dd_leave.value
            if leave_val == "None": leave_val = None

            s_val = txt_start.value.strip()
            e_val = txt_end.value.strip()
            
            # REVERTED: Restored validation
            if s_val and len(s_val) != 5: raise ValueError("Start Time must be HH:MM")
            if e_val and len(e_val) != 5: raise ValueError("End Time must be HH:MM")

            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("""
                INSERT INTO offline_queue 
                (day_date, start_time, end_time, leave_type, ojti_hours, cic_hours, timestamp) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (txt_date.value, s_val, e_val, leave_val, ojti, cic, datetime.now().isoformat()))
            conn.commit()
            conn.close()

            lbl_status.value = f"Saved {txt_date.value}"
            lbl_status.color = "green"
            
        except ValueError as ve:
            lbl_status.value = str(ve)
            lbl_status.color = "red"
        except Exception as err:
            lbl_status.value = f"Error: {str(err)}"
            lbl_status.color = "red"
        
        page.update()

    def sync_to_pc_click(e):
        lbl_status.value = "Syncing..."
        page.update()

        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        rows = c.execute("SELECT * FROM offline_queue").fetchall()
        if not rows:
            lbl_status.value = "Queue empty."
            conn.close()
            page.update()
            return

        payload = [dict(row) for row in rows]

        try:
            # CHANGED: Appending endpoint to base URL
            response = requests.post(f"{DESKTOP_URL}/mobile_sync", json=payload, timeout=5)

            if response.status_code == 200:
                c.execute("DELETE FROM offline_queue")
                conn.commit()
                lbl_status.value = f"Synced {len(rows)} entries."
                lbl_status.color = "green"
            else:
                # REVERTED: Variable name 'response'
                lbl_status.value = f"Server Error: {response.status_code}"
                lbl_status.color = "red"

        except Exception as err:
            lbl_status.value = "Sync Failed."
            lbl_status.color = "red"
        finally:
            conn.close()
            page.update()

    # NEW: Function to get defaults
    def get_defaults_click(e):
        lbl_status.value = "Downloading defaults..."
        page.update()
        try:
            # Calls the new endpoint
            response = requests.get(f"{DESKTOP_URL}/get_schedule_defaults", timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute("DELETE FROM schedule_defaults")
                
                for day_idx, times in data.items():
                    c.execute("INSERT INTO schedule_defaults (day_idx, start_time, end_time) VALUES (?, ?, ?)",
                              (int(day_idx), times['start'], times['end']))
                conn.commit()
                conn.close()
                lbl_status.value = "Defaults updated!"
                lbl_status.color = "green"
            else:
                lbl_status.value = f"Error: {response.status_code}"
                lbl_status.color = "red"
        except Exception as err:
            lbl_status.value = "Connection Failed"
            lbl_status.color = "red"
        page.update()

    # --- LAYOUT ---
    page.add(
        ft.Column([
            ft.Text("Add Shift", size=20, weight="bold"),
            ft.Row([txt_date, btn_pick_date]),
            ft.Divider(),
            ft.Row([txt_start, txt_end], alignment="spaceBetween"),
            dd_leave,
            ft.Row([txt_ojti, txt_cic], alignment="spaceBetween"),
            ft.Divider(),
            ft.ElevatedButton("Save Local", icon=ft.Icons.SAVE, on_click=save_local_click, width=400),
            ft.ElevatedButton("Sync to PC", icon=ft.Icons.WIFI, on_click=sync_to_pc_click, width=400),
            # NEW BUTTON
            ft.ElevatedButton("Get Defaults", icon=ft.Icons.DOWNLOAD, on_click=get_defaults_click, width=400),
            ft.Container(height=10),
            lbl_status
        ])
    )

if __name__ == "__main__":
    ft.app(target=main)
