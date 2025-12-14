import flet as ft
import sqlite3
import requests
import json
from datetime import datetime

# --- CONFIGURATION ---
DESKTOP_URL = "http://10.0.0.77:5000/mobile_sync"
DB_NAME = "mobile_data.db"

def init_db():
    """Initialize local DB matching the timesheet_entry_v2 schema."""
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

    def change_date(e):
        txt_date.value = date_picker.value.strftime("%Y-%m-%d")
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
        on_click=open_picker  # Use the new handler here
    )

    # 2. Shift Times (Start/End)
    txt_start = ft.TextField(label="Start (HH:MM)", hint_text="14:30", width=160)
    txt_end = ft.TextField(label="End (HH:MM)", hint_text="22:30", width=160)

    # 3. Leave Type (Matches dashboard.py options)
    dd_leave = ft.Dropdown(
        label="Leave Type (Optional)",
        options=[
            ft.dropdown.Option("None"), # Maps to None/Work
            ft.dropdown.Option("Annual"),
            ft.dropdown.Option("Sick"),
            ft.dropdown.Option("Holiday"), # Added per your request
            ft.dropdown.Option("Credit"),
            ft.dropdown.Option("Comp"),
            ft.dropdown.Option("LWOP"),
        ],
        value="None"
    )

    # 4. Differentials
    txt_ojti = ft.TextField(label="OJTI (Hrs)", value="0", keyboard_type=ft.KeyboardType.NUMBER, width=160)
    txt_cic = ft.TextField(label="CIC (Hrs)", value="0", keyboard_type=ft.KeyboardType.NUMBER, width=160)

    # --- ACTIONS ---

    def save_local_click(e):
        try:
            # Basic validation
            ojti = float(txt_ojti.value) if txt_ojti.value else 0.0
            cic = float(txt_cic.value) if txt_cic.value else 0.0
            
            leave_val = dd_leave.value
            if leave_val == "None": leave_val = None

            # Enforce 24h time format length check
            s_val = txt_start.value.strip()
            e_val = txt_end.value.strip()
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
            lbl_status.color="green"
            
        except ValueError as ve:
            lbl_status.value = str(ve)
            lbl_status.color="red"
        except Exception as err:
            lbl_status.value = f"Error: {str(err)}"
            lbl_status.color="red"
        
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
            # Post to new desktop endpoint
            response = requests.post(DESKTOP_URL, json=payload, timeout=5)

            if response.status_code == 200:
                c.execute("DELETE FROM offline_queue")
                conn.commit()
                lbl_status.value = f"Synced {len(rows)} entries."
                lbl_status.color="green"
            else:
                lbl_status.value = f"Server Error: {response.status_code}"
                lbl_status.color="red"

        except Exception as err:
            lbl_status.value = "Sync Failed. Check Wi-Fi / IP."
            lbl_status.color="red"
        finally:
            conn.close()
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
            ft.Container(height=10),
            lbl_status
        ])
    )

if __name__ == "__main__":
    ft.app(target=main)
