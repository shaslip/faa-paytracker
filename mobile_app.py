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

# --- THEME COLORS ---
PRIMARY_COLOR = ft.Colors.INDIGO
BG_COLOR = ft.Colors.GREY_100
CARD_BG = ft.Colors.WHITE

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
    # NEW: Cache for holidays
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
    page.theme = ft.Theme(color_scheme_seed=PRIMARY_COLOR)
    page.bgcolor = BG_COLOR
    page.window_width = 400
    page.window_height = 800
    
    init_db()

    # --- SHARED UI COMPONENTS ---
    snack_bar = ft.SnackBar(content=ft.Text(""))
    page.overlay.append(snack_bar)

    def show_msg(msg, is_error=False):
        snack_bar.content.value = msg
        snack_bar.bgcolor = ft.Colors.RED_700 if is_error else ft.Colors.GREEN_700
        snack_bar.open = True
        page.update()

    # ==========================================
    # TAB 1: ADD SHIFT (Redesigned)
    # ==========================================
    
    # -- 1. Date Input --
    txt_date = ft.TextField(
        label="Date", 
        value=datetime.now().strftime("%Y-%m-%d"), 
        read_only=True,
        icon=ft.Icons.CALENDAR_TODAY,
        expand=True,
        border_radius=10,
        filled=True
    )

    def auto_colon(e):
        prev_len = e.control.data if e.control.data is not None else 0
        val = e.control.value
        if len(val) == 2 and len(val) > prev_len and val.isdigit():
            e.control.value = val + ":"
            e.control.update()
        e.control.data = len(e.control.value)

    def change_date(e):
        new_date = date_picker.value
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
            show_msg("Shift hours auto-filled")
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
        icon=ft.Icons.EDIT_CALENDAR,
        icon_color=PRIMARY_COLOR,
        on_click=lambda _: setattr(date_picker, 'open', True) or page.update()
    )

    # -- 2. Time Inputs --
    txt_start = ft.TextField(label="Start", hint_text="07:00", width=140, on_change=auto_colon, 
                             icon=ft.Icons.ACCESS_TIME, border_radius=10, filled=True)
    txt_end = ft.TextField(label="End", hint_text="15:00", width=140, on_change=auto_colon, 
                           icon=ft.Icons.ACCESS_TIME_FILLED, border_radius=10, filled=True)

    # -- 3. Leave Dropdown --
    dd_leave = ft.Dropdown(
        label="Leave Type",
        prefix_icon=ft.Icons.FLIGHT_TAKEOFF,
        options=[
            ft.dropdown.Option("None"), ft.dropdown.Option("Annual"),
            ft.dropdown.Option("Sick"), ft.dropdown.Option("Holiday"),
            ft.dropdown.Option("Credit"), ft.dropdown.Option("Comp"),
            ft.dropdown.Option("LWOP"),
        ],
        value="None",
        border_radius=10,
        filled=True
    )

    # -- 4. Differentials --
    txt_ojti = ft.TextField(label="OJTI", width=140, on_change=auto_colon, 
                            icon=ft.Icons.HEADSET_MIC, border_radius=10, filled=True)
    txt_cic = ft.TextField(label="CIC", width=140, on_change=auto_colon, 
                           icon=ft.Icons.SUPERVISOR_ACCOUNT, border_radius=10, filled=True)

    # -- Actions --
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
            show_msg(f"Saved shift for {txt_date.value}")
        except Exception as err:
            show_msg(str(err), is_error=True)

    def sync_data(e):
        show_msg("Syncing...")
        try:
            # 1. Sync Queue
            conn = sqlite3.connect(DB_NAME)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM offline_queue").fetchall()
            
            if rows:
                payload = [dict(r) for r in rows]
                r = requests.post(f"{DESKTOP_URL}/mobile_sync", json=payload, timeout=5)
                if r.status_code == 200:
                    conn.execute("DELETE FROM offline_queue")
                    conn.commit()
                    show_msg(f"Synced {len(rows)} shifts to PC")
                else:
                    show_msg(f"Sync Error: {r.status_code}", True)
            else:
                show_msg("Shift queue is empty")
            
            # 2. Update Schedule Defaults (Silent update)
            r_sched = requests.get(f"{DESKTOP_URL}/get_schedule_defaults", timeout=5)
            if r_sched.status_code == 200:
                conn.execute("DELETE FROM schedule_defaults")
                for i in r_sched.json():
                    conn.execute("INSERT INTO schedule_defaults VALUES (?,?,?,?)", 
                                 (i['year'], i['day'], i['start'], i['end']))
                conn.commit()

            conn.close()
        except Exception as err:
            show_msg(f"Connection Failed: {err}", True)

    # Layout for Tab 1
    tab_shift_content = ft.Container(
        padding=20,
        content=ft.Column([
            ft.Container(
                bgcolor=CARD_BG,
                padding=20,
                border_radius=15,
                shadow=ft.BoxShadow(spread_radius=1, blur_radius=10, color=ft.Colors.GREY_300),
                content=ft.Column([
                    ft.Text("New Entry", size=20, weight="bold", color=PRIMARY_COLOR),
                    ft.Divider(height=20, color="transparent"),
                    ft.Row([txt_date, btn_pick_date]),
                    ft.Row([txt_start, txt_end], alignment="spaceBetween"),
                    dd_leave,
                    ft.Row([txt_ojti, txt_cic], alignment="spaceBetween"),
                    ft.Divider(height=20),
                    ft.ElevatedButton(
                        "Save Entry", 
                        icon=ft.Icons.SAVE, 
                        on_click=save_local_click, 
                        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10), padding=15),
                        width=400
                    )
                ])
            ),
            ft.Container(height=10),
            ft.Row([
                ft.OutlinedButton("Sync & Update", icon=ft.Icons.SYNC, on_click=sync_data, expand=True),
            ])
        ])
    )

    # ==========================================
    # TAB 2: HOLIDAYS
    # ==========================================
    
    # DataTable to hold the rows
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
        current_year = datetime.now().year
        # Fetch current year or next year
        rows = conn.execute(
            "SELECT name, date, day FROM holiday_cache WHERE year >= ? ORDER BY date", 
            (current_year,)
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

    def fetch_holidays_click(e):
        show_msg("Fetching calculated holidays...")
        try:
            # Fetch for current year AND next year to be safe
            years_to_fetch = [datetime.now().year, datetime.now().year + 1]
            conn = sqlite3.connect(DB_NAME)
            conn.execute("DELETE FROM holiday_cache") # Clear old cache
            
            for y in years_to_fetch:
                r = requests.get(f"{DESKTOP_URL}/get_holidays?year={y}", timeout=5)
                if r.status_code == 200:
                    data = r.json()
                    for h in data:
                        conn.execute("INSERT INTO holiday_cache VALUES (?,?,?,?)", 
                                     (h['year'], h['name'], h['date'], h['day']))
            
            conn.commit()
            conn.close()
            load_holidays_from_db()
            show_msg("Holidays updated!", False)
        except Exception as err:
            show_msg(f"Failed: {err}", True)

    # Layout for Tab 2
    tab_holidays_content = ft.Container(
        padding=10,
        content=ft.Column([
            ft.Container(
                bgcolor=CARD_BG,
                padding=10,
                border_radius=15,
                shadow=ft.BoxShadow(spread_radius=1, blur_radius=5, color=ft.Colors.GREY_300),
                content=ft.Column([
                    ft.Row([
                        ft.Text("Observed Dates", size=18, weight="bold", color=PRIMARY_COLOR),
                        ft.IconButton(ft.Icons.REFRESH, icon_color=PRIMARY_COLOR, on_click=fetch_holidays_click)
                    ], alignment="spaceBetween"),
                    ft.Divider(),
                    ft.Column([holiday_table], scroll=ft.ScrollMode.ADAPTIVE, height=500)
                ])
            )
        ])
    )

    # --- MAIN TABS ---
    t = ft.Tabs(
        selected_index=0,
        animation_duration=300,
        tabs=[
            ft.Tab(
                text="Add Shift",
                icon=ft.Icons.ADD_TASK,
                content=tab_shift_content
            ),
            ft.Tab(
                text="Holidays",
                icon=ft.Icons.CELEBRATION,
                content=tab_holidays_content
            ),
        ],
        expand=1,
    )

    page.add(t)
    
    # Load initial data
    load_holidays_from_db()

if __name__ == "__main__":
    ft.app(target=main)
