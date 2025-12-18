import flet as ft
import sqlite3
import requests
import json
import os
from datetime import datetime

# --- CONFIGURATION ---
DEFAULT_IP = "http://10.0.0.77:5000"

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
    c.execute('''
        CREATE TABLE IF NOT EXISTS server_actuals (
            day_date TEXT PRIMARY KEY,
            start_time TEXT,
            end_time TEXT,
            leave_type TEXT,
            ojti_hours REAL,
            cic_hours REAL
        )
    ''')
    conn.commit()
    conn.close()

def main(page: ft.Page):
    APP_VERSION = "1.2"
    UPDATE_URL = "https://ee-paytracker.s3.us-east-1.amazonaws.com/version.json"
    page.title = "FAA PayTracker"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.window_width = 400
    page.window_height = 800
    
    init_db()

    # --- SETTINGS LOGIC ---
    stored_ip = page.client_storage.get("server_ip")
    current_ip = stored_ip if stored_ip else DEFAULT_IP

    def check_for_update():
        try:
            # Short timeout so app doesn't hang if offline
            r = requests.get(UPDATE_URL, timeout=3)
            if r.status_code == 200:
                data = r.json()
                latest = data.get("latest_version", "0.0.0")
                apk_url = data.get("apk_url", "")

                # Simple string compare (or use packaging.version for robust handling)
                if latest > APP_VERSION:
                    show_update_dialog(latest, apk_url)
        except:
            pass # Fail silently if offline

    def show_update_dialog(new_ver, url):
        def dl_update(e):
            # This opens the system browser to handle the download & install prompt
            page.launch_url(url)
            update_dialog.open = False
            page.update()

        update_dialog = ft.AlertDialog(
            title=ft.Text("Update Available"),
            content=ft.Text(f"Version {new_ver} is available."),
            actions=[
                ft.TextButton("Update Now", on_click=dl_update),
                ft.TextButton("Later", on_click=lambda e: setattr(update_dialog, 'open', False) or page.update()),
            ],
        )
        page.overlay.append(update_dialog)
        update_dialog.open = True
        page.update()

    def get_url():
        return current_ip

    def save_settings(e):
        nonlocal current_ip
        new_ip = txt_ip.value.strip()
        if not new_ip.startswith("http"):
            new_ip = "http://" + new_ip
        
        page.client_storage.set("server_ip", new_ip)
        current_ip = new_ip
        settings_dialog.open = False
        lbl_status.value = f"IP Saved: {current_ip}"
        lbl_status.color = "blue"
        page.update()

    txt_ip = ft.TextField(label="Server URL", value=current_ip)
    settings_dialog = ft.AlertDialog(
        title=ft.Text("Settings"),
        content=ft.Column([
            txt_ip,
            ft.Container(height=10),
            ft.Text(f"Version: {APP_VERSION}", size=12, color=ft.Colors.GREY_500)
        ], tight=True, width=300),  # tight=True makes it fit content height
        actions=[
            ft.TextButton("Save", on_click=save_settings),
            ft.TextButton("Cancel", on_click=lambda e: setattr(settings_dialog, 'open', False) or page.update()),
        ],
    )
    page.overlay.append(settings_dialog)

    page.appbar = ft.AppBar(
        title=ft.Text("FAA PayTracker"),
        center_title=False,
        bgcolor=ft.Colors.BLUE_GREY_50,
        actions=[
            ft.IconButton(ft.Icons.SETTINGS, on_click=lambda e: setattr(settings_dialog, 'open', True) or page.update())
        ],
    )

    lbl_status = ft.Text(value="Ready", color="grey")

    # ==========================================
    # TAB 1: ADD SHIFT
    # ==========================================
    
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
        if date_picker.value:
            new_date = date_picker.value
        else:
            try:
                new_date = datetime.strptime(txt_date.value, "%Y-%m-%d")
            except:
                new_date = datetime.now()

        txt_date.value = new_date.strftime("%Y-%m-%d")
        
        day_idx = new_date.weekday()
        target_year = new_date.year
        date_str = txt_date.value
        
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row 
        
        row_q = conn.execute("SELECT * FROM offline_queue WHERE day_date=?", (date_str,)).fetchone()
        row_act = conn.execute("SELECT * FROM server_actuals WHERE day_date=?", (date_str,)).fetchone()
        row_def = conn.execute("SELECT start_time, end_time FROM schedule_defaults WHERE year=? AND day_idx=?", (target_year, day_idx)).fetchone()
        
        conn.close()

        if row_q:
            txt_start.value = row_q['start_time'] if row_q['start_time'] else ""
            txt_end.value = row_q['end_time'] if row_q['end_time'] else ""
            txt_ojti.value = str(row_q['ojti_hours']) if row_q['ojti_hours'] > 0 else ""
            txt_cic.value = str(row_q['cic_hours']) if row_q['cic_hours'] > 0 else ""
            dd_leave.value = row_q['leave_type'] if row_q['leave_type'] else "None"
            lbl_status.value = "Loaded local draft."
            lbl_status.color = "orange"
        elif row_act:
            txt_start.value = row_act['start_time'] if row_act['start_time'] else ""
            txt_end.value = row_act['end_time'] if row_act['end_time'] else ""
            txt_ojti.value = str(row_act['ojti_hours']) if row_act['ojti_hours'] > 0 else ""
            txt_cic.value = str(row_act['cic_hours']) if row_act['cic_hours'] > 0 else ""
            dd_leave.value = row_act['leave_type'] if row_act['leave_type'] else "None"
            lbl_status.value = "Loaded from Desktop."
            lbl_status.color = "blue"
        elif row_def:
            txt_start.value = row_def['start_time'] if row_def['start_time'] else ""
            txt_end.value = row_def['end_time'] if row_def['end_time'] else ""
            txt_ojti.value = ""
            txt_cic.value = ""
            dd_leave.value = "None"
            lbl_status.value = "Standard Schedule."
            lbl_status.color = "grey"
        else:
            txt_start.value = ""
            txt_end.value = ""
            txt_ojti.value = ""
            txt_cic.value = ""
            dd_leave.value = "None"
            
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
            
            load_pending_queue()
            
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
                r = requests.post(f"{get_url()}/mobile_sync", json=payload, timeout=5)
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
            
            load_pending_queue()
            
        except Exception as err:
            lbl_status.value = f"Connection Failed: {str(err)}"
            lbl_status.color = "red"
        page.update()

    def get_updates_click(e):
        lbl_status.value = "Downloading Data..."
        page.update()
        try:
            # 1. Defaults
            r_sched = requests.get(f"{get_url()}/get_schedule_defaults", timeout=5)
            conn = sqlite3.connect(DB_NAME)
            if r_sched.status_code == 200:
                conn.execute("DELETE FROM schedule_defaults")
                for i in r_sched.json():
                    conn.execute("INSERT INTO schedule_defaults VALUES (?,?,?,?)", 
                                 (i['year'], i['day'], i['start'], i['end']))
            
            # 2. Saved Shifts
            r_shifts = requests.get(f"{get_url()}/get_saved_shifts?year={datetime.now().year}", timeout=5)
            if r_shifts.status_code == 200:
                conn.execute("DELETE FROM server_actuals")
                for s in r_shifts.json():
                    conn.execute("""
                        INSERT INTO server_actuals (day_date, start_time, end_time, leave_type, ojti_hours, cic_hours)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (s['date'], s['start'], s['end'], s['leave'], s['ojti'], s['cic']))

            # 3. Holidays
            conn.execute("DELETE FROM holiday_cache")
            years = [datetime.now().year, datetime.now().year + 1]
            for y in years:
                r_hol = requests.get(f"{get_url()}/get_holidays?year={y}", timeout=5)
                if r_hol.status_code == 200:
                    for h in r_hol.json():
                        conn.execute("INSERT INTO holiday_cache VALUES (?,?,?,?)", 
                                     (h['year'], h['name'], h['date'], h['day']))
            
            conn.commit()
            conn.close()
            lbl_status.value = "Updates Downloaded!"
            lbl_status.color = "green"
            
            load_holidays_from_db()
            change_date(None)
            
        except Exception as err:
            lbl_status.value = f"Error: {str(err)}"
            lbl_status.color = "red"
        page.update()

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
                ft.ElevatedButton("Download schedule", icon=ft.Icons.DOWNLOAD, on_click=get_updates_click, expand=True),
            ]),
            ft.Container(height=10),
            lbl_status
        ])
    )

    # ==========================================
    # TAB 2: HOLIDAYS
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

    # ==========================================
    # TAB 3: PENDING (RAW DUMP)
    # ==========================================
    pending_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("Date")),
            ft.DataColumn(ft.Text("Start")),
            ft.DataColumn(ft.Text("End")),
            ft.DataColumn(ft.Text("Leave")),
            ft.DataColumn(ft.Text("OJTI")), 
            ft.DataColumn(ft.Text("CIC")),  
        ],
        width=400,
        heading_row_color=ft.Colors.GREY_200,
        column_spacing=10
    )

    def load_pending_queue():
        conn = sqlite3.connect(DB_NAME)
        # Fetch everything exactly as it is in the queue
        rows = conn.execute("SELECT day_date, start_time, end_time, leave_type, ojti_hours, cic_hours FROM offline_queue ORDER BY day_date DESC").fetchall()
        conn.close()

        pending_table.rows.clear()
        
        # Helper: Convert 1.5 -> "1:30"
        def fmt_hours(val):
            if not val or val <= 0: return "-"
            h = int(val)
            m = int(round((val - h) * 60))
            return f"{h}:{m:02d}"

        for d, s, e, l, o, c in rows:
            # Display "-" for blanks, but show ALL data regardless of defaults
            s_disp = s if s else "-"
            e_disp = e if e else "-"
            l_disp = l if l and l != "None" else "-"
            
            # Apply the formatting here
            o_disp = fmt_hours(o)
            c_disp = fmt_hours(c)

            pending_table.rows.append(
                ft.DataRow(cells=[
                    ft.DataCell(ft.Text(d, size=12, weight="bold")),
                    ft.DataCell(ft.Text(s_disp, size=12)),
                    ft.DataCell(ft.Text(e_disp, size=12)),
                    ft.DataCell(ft.Text(l_disp, size=12)),
                    ft.DataCell(ft.Text(o_disp, size=12)), 
                    ft.DataCell(ft.Text(c_disp, size=12)),  
                ])
            )
        
        page.update()

    tab_pending_content = ft.Container(
        padding=10,
        content=ft.Column([
            ft.Text("Pending Sync Queue", size=20, weight="bold"),
            ft.Divider(),
            ft.Column([pending_table], scroll=ft.ScrollMode.ADAPTIVE, height=600)
        ])
    )

    # --- MAIN TABS ---
    t = ft.Tabs(
        selected_index=0,
        animation_duration=300,
        tabs=[
            ft.Tab(text="Add Shift", icon=ft.Icons.ADD_TASK, content=tab_shift_content),
            ft.Tab(text="Pending", icon=ft.Icons.PENDING_ACTIONS, content=tab_pending_content),
            ft.Tab(text="Holidays", icon=ft.Icons.CALENDAR_MONTH, content=tab_holidays_content),
        ],
        expand=1,
    )

    page.add(t)
    load_holidays_from_db()
    load_pending_queue()
    change_date(None)

if __name__ == "__main__":
    ft.app(target=main)
