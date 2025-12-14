import flet as ft
import sqlite3
import requests
import json
import os
from datetime import datetime

# --- CONFIGURATION ---
DESKTOP_URL = "http://10.0.0.77:5000" 
if "ANDROID_ARGUMENT" in os.environ:
    files_dir = os.environ.get("EXTERNAL_FILES_DIR", ".")
    DB_NAME = os.path.join(files_dir, "mobile_data.db")
else:
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
    c.execute('''
        CREATE TABLE IF NOT EXISTS schedule_defaults (
            year INTEGER,
            day_idx INTEGER,
            start_time TEXT,
            end_time TEXT,
            PRIMARY KEY (year, day_idx)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS holiday_cache (
            year INTEGER,
            name TEXT,
            date TEXT,
            day TEXT
        )
    ''')
    conn.commit()
    conn.close()

def main(page: ft.Page):
    page.title = "FAA PayTracker"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.window_width = 400
    page.window_height = 800
    
    init_db()

    # --- SHARED UI COMPONENTS ---
    lbl_status = ft.Text(value="Ready", color="grey")

    # ==========================================
    # TAB 1: ADD SHIFT (Restored Functional UI)
    # ==========================================
    
    # 1. Date
    txt_date = ft.TextField(
        label="Date", 
        value=datetime.now().strftime("%Y-%m-%d"), 
        read_only=True,
        expand=True
    )

    def auto_colon(e):
        prev_len = e.control.data if e.control.data is not None else 0
        val = e.control.value
        if len(val) == 2 and len(val) > prev_len and val.isdigit():
            e.control.value = val + ":"
            e.control.update()
        e.control.data = len(e.control.value)

    def change_date(e):
        # FIX: Handle case where date_picker.value is None (e.g., app startup or programmatic call)
        if date_picker.value:
            new_date = date_picker.value
        else:
            # Fallback to the text field or today
            try:
                new_date = datetime.strptime(txt_date.value, "%Y-%m-%d")
            except:
                new_date = datetime.now()

        txt_date.value = new_date.strftime("%Y-%m-%d")
        
        # Auto-Fill Logic
        day_idx = new_date.weekday()
        target_year = new_date.year
        conn = sqlite3.connect(DB_NAME)
        row = conn.execute(
            "SELECT start_time, end_time FROM schedule_defaults WHERE year=? AND day_idx=?", 
            (target_year, day_idx)
        ).fetchone()
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

    btn_pick_date = ft.IconButton(
        icon=ft.Icons.CALENDAR_MONTH,
        on_click=lambda _: setattr(date_picker, 'open', True) or page.update()
    )

    # 2. Inputs
    txt_start = ft.TextField(label="Start (HH:MM)", hint_text="07:00", width=160, on_change=auto_colon)
    txt_end = ft.TextField(label="End (HH:MM)", hint_text="15:00", width=160, on_change=auto_colon)

    dd_leave = ft.Dropdown(
        label="Leave Type (Optional)",
        options=[
            ft.dropdown.Option("None"), ft.dropdown.Option("Annual"),
            ft.dropdown.Option("Sick"), ft.dropdown.Option("Holiday"),
            ft.dropdown.Option("Credit"), ft.dropdown.Option("Comp"),
            ft.dropdown.Option("LWOP"),
        ],
        value="None"
    )

    txt_ojti = ft.TextField(label="OJTI (HH:MM)", width=160, on_change=auto_colon)
    txt_cic = ft.TextField(label="CIC (HH:MM)", width=160, on_change=auto_colon)

    # 3. Actions
    def save_local_click(e):
        try:
            def parse_time(val):
                val = val.strip()
                if not val: return 0.0
                if ":" in val:
                    parts = val.split(":")
                    return float(parts[0]) + (float(parts[1]) / 60.0)
                return float(val)

            ojti = parse_time(txt_ojti.value)
            cic = parse_time(txt_cic.value)
            leave_val = dd_leave.value if dd_leave.value != "None" else None
            s_val = txt_start.value.strip()
            e_val = txt_end.value.strip()

            if s_val and len(s_val) != 5: raise ValueError("Start Time must be HH:MM")
            if e_val and len(e_val) != 5: raise ValueError("End Time must be HH:MM")

            conn = sqlite3.connect(DB_NAME)
            conn.execute("""
                INSERT INTO offline_queue 
                (day_date, start_time, end_time, leave_type, ojti_hours, cic_hours, timestamp) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (txt_date.value, s_val, e_val, leave_val, ojti, cic, datetime.now().isoformat()))
            conn.commit()
            conn.close()

            lbl_status.value = f"Saved {txt_date.value}"
            lbl_status.color = "green"
        except Exception as err:
            lbl_status.value = f"Error: {str(err)}"
            lbl_status.color = "red"
        page.update()

    def sync_to_pc_click(e):
        lbl_status.value = "Syncing Shifts..."
        page.update()
        try:
            conn = sqlite3.connect(DB_NAME)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM offline_queue").fetchall()
            
            if rows:
                payload = [dict(r) for r in rows]
                r = requests.post(f"{DESKTOP_URL}/mobile_sync", json=payload, timeout=5)
                if r.status_code == 200:
                    conn.execute("DELETE FROM offline_queue")
                    conn.commit()
                    lbl_status.value = f"Synced {len(rows)} entries."
                    lbl_status.color = "green"
                else:
                    lbl_status.value = f"Server Error: {r.status_code}"
                    lbl_status.color = "red"
            else:
                lbl_status.value = "Queue empty."
            conn.close()
        except Exception as err:
            lbl_status.value = "Connection Failed"
            lbl_status.color = "red"
        page.update()

    def get_updates_click(e):
        lbl_status.value = "Downloading Schedule & Holidays..."
        page.update()
        try:
            # 1. Get Schedule Defaults
            r_sched = requests.get(f"{DESKTOP_URL}/get_schedule_defaults", timeout=5)
            conn = sqlite3.connect(DB_NAME)
            
            if r_sched.status_code == 200:
                conn.execute("DELETE FROM schedule_defaults")
                for i in r_sched.json():
                    conn.execute("INSERT INTO schedule_defaults VALUES (?,?,?,?)", 
                                 (i['year'], i['day'], i['start'], i['end']))
            
            # 2. Get Holidays (Current + Next Year)
            conn.execute("DELETE FROM holiday_cache")
            years = [datetime.now().year, datetime.now().year + 1]
            for y in years:
                r_hol = requests.get(f"{DESKTOP_URL}/get_holidays?year={y}", timeout=5)
                if r_hol.status_code == 200:
                    for h in r_hol.json():
                        conn.execute("INSERT INTO holiday_cache VALUES (?,?,?,?)", 
                                     (h['year'], h['name'], h['date'], h['day']))
            
            conn.commit()
            conn.close()
            
            lbl_status.value = "Updates Downloaded!"
            lbl_status.color = "green"
            
            # Refresh views
            load_holidays_from_db()
            change_date(None)
            
        except Exception as err:
            # FIX: Show the ACTUAL error message
            lbl_status.value = f"Error: {str(err)}"
            lbl_status.color = "red"
        page.update()

    # Layout Tab 1
    tab_shift_content = ft.Container(
        padding=10,
        content=ft.Column([
            ft.Text("Add Shift", size=20, weight="bold"),
            ft.Row([txt_date, btn_pick_date]),
            ft.Divider(),
            ft.Row([txt_start, txt_end], alignment="spaceBetween"),
            dd_leave,
            ft.Row([txt_ojti, txt_cic], alignment="spaceBetween"),
            ft.Divider(),
            ft.ElevatedButton("Save Local", icon=ft.Icons.SAVE, on_click=save_local_click, width=400),
            ft.Row([
                ft.ElevatedButton("Sync to PC", icon=ft.Icons.UPLOAD, on_click=sync_to_pc_click, expand=True),
                ft.ElevatedButton("Get Updates", icon=ft.Icons.DOWNLOAD, on_click=get_updates_click, expand=True),
            ]),
            ft.Container(height=10),
            lbl_status
        ])
    )

    # ==========================================
    # TAB 2: HOLIDAYS (Functional List)
    # ==========================================
    
    holiday_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("Holiday")),
            ft.DataColumn(ft.Text("Observed")),
            ft.DataColumn(ft.Text("Day")),
        ],
        width=400,
        heading_row_color=ft.Colors.GREY_200,
    )

    def load_holidays_from_db():
        conn = sqlite3.connect(DB_NAME)
        rows = conn.execute(
            "SELECT name, date, day FROM holiday_cache WHERE year >= ? ORDER BY date", 
            (datetime.now().year,)
        ).fetchall()
        conn.close()
        
        holiday_table.rows.clear()
        for name, date, day in rows:
            holiday_table.rows.append(
                ft.DataRow(cells=[
                    ft.DataCell(ft.Text(name, size=12)),
                    ft.DataCell(ft.Text(date, weight="bold")),
                    ft.DataCell(ft.Text(day, size=12)),
                ])
            )
        page.update()

    tab_holidays_content = ft.Container(
        padding=10,
        content=ft.Column([
            ft.Text("My Observed Holidays", size=20, weight="bold"),
            ft.Divider(),
            ft.Column([holiday_table], scroll=ft.ScrollMode.ADAPTIVE, height=600)
        ])
    )

    # --- MAIN TABS ---
    t = ft.Tabs(
        selected_index=0,
        animation_duration=300,
        tabs=[
            ft.Tab(text="Add Shift", icon=ft.Icons.ADD_TASK, content=tab_shift_content),
            ft.Tab(text="Holidays", icon=ft.Icons.CALENDAR_MONTH, content=tab_holidays_content),
        ],
        expand=1,
    )

    page.add(t)
    load_holidays_from_db()

if __name__ == "__main__":
    ft.app(target=main)
