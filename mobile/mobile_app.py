import flet as ft
import sqlite3
import requests
import json
import os
from datetime import datetime, timedelta

# --- CONFIGURATION ---
DEFAULT_IP = "http://10.0.0.77:5000"

if "ANDROID_ARGUMENT" in os.environ:
    files_dir = os.environ.get("EXTERNAL_FILES_DIR", ".")
    DB_NAME = os.path.join(files_dir, "mobile_data.db")
else:
    DB_NAME = "mobile_data.db"

def init_db():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    try:
        c.execute('''
            CREATE TABLE IF NOT EXISTS offline_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_date TEXT, start_time TEXT, end_time TEXT,
                leave_type TEXT, ojti_hours REAL, cic_hours REAL, timestamp TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS schedule_defaults (
                year INTEGER, day_idx INTEGER,
                start_time TEXT, end_time TEXT,
                PRIMARY KEY (year, day_idx)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS holiday_cache (
                year INTEGER, name TEXT, date TEXT, day TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS server_actuals (
                day_date TEXT PRIMARY KEY, start_time TEXT, end_time TEXT,
                leave_type TEXT, ojti_hours REAL, cic_hours REAL
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS paystub_summary (
                type TEXT, label TEXT, value1 REAL, value2 TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS ytd_stats (
                year INTEGER, category TEXT, type TEXT, amount REAL, hours REAL
            )
        ''')
        conn.commit()
    finally:
        conn.close()

def main(page: ft.Page):
    APP_VERSION = "1.3.4"
    UPDATE_URL = "https://raw.githubusercontent.com/shaslip/faa-paytracker/main/mobile/version.json"
    page.title = "FAA PayTracker"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.window_width = 400
    page.window_height = 800
    
    init_db()

    # --- SETTINGS LOGIC ---
    stored_ip = page.client_storage.get("server_ip")
    current_ip = stored_ip if stored_ip else DEFAULT_IP

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
        ], tight=True, width=300),
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
    # LOCAL MATH ENGINE (Estimates Gross Bump)
    # ==========================================
    def calculate_pending_bump():
        conn = sqlite3.connect(DB_NAME, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rate_row = conn.execute("SELECT value1 FROM paystub_summary WHERE type='rate'").fetchone()
            base_rate = rate_row['value1'] if rate_row else 0.0
            if base_rate <= 0: return 0.0

            queue = conn.execute("SELECT * FROM offline_queue").fetchall()
            bump = 0.0

            def time_diff(start_str, end_str):
                if not start_str or not end_str: return 0.0
                s = datetime.strptime(start_str, "%H:%M")
                e = datetime.strptime(end_str, "%H:%M")
                if e < s: e += timedelta(days=1)
                return (e - s).total_seconds() / 3600.0

            for q in queue:
                dt = datetime.strptime(q['day_date'], "%Y-%m-%d")
                day_idx = dt.weekday()
                target_year = dt.year
                
                def_row = conn.execute("SELECT start_time, end_time FROM schedule_defaults WHERE year=? AND day_idx=?", (target_year, day_idx)).fetchone()
                
                std_hours = 0.0
                if def_row and def_row['start_time'] and def_row['end_time']:
                    std_hours = time_diff(def_row['start_time'], def_row['end_time'])
                    
                act_hours = time_diff(q['start_time'], q['end_time'])
                
                # Overtime is anything beyond standard hours
                ot_hours = max(0.0, act_hours - std_hours)
                
                ojti = q['ojti_hours'] if q['ojti_hours'] else 0.0
                cic = q['cic_hours'] if q['cic_hours'] else 0.0
                
                bump += (ot_hours * base_rate * 1.5)
                bump += (ojti * base_rate * 0.25)
                bump += (cic * base_rate * 0.10)
                
            return bump
        finally:
            conn.close()

    # ==========================================
    # TAB 1: ADD SHIFT
    # ==========================================
    txt_date = ft.TextField(label="Date", value=datetime.now().strftime("%Y-%m-%d"), read_only=True, expand=True)

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
        
        conn = sqlite3.connect(DB_NAME, timeout=10)
        conn.row_factory = sqlite3.Row 
        
        try:
            row_q = conn.execute("SELECT * FROM offline_queue WHERE day_date=?", (date_str,)).fetchone()
            row_act = conn.execute("SELECT * FROM server_actuals WHERE day_date=?", (date_str,)).fetchone()
            row_def = conn.execute("SELECT start_time, end_time FROM schedule_defaults WHERE year=? AND day_idx=?", (target_year, day_idx)).fetchone()
        finally:
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

            conn = sqlite3.connect(DB_NAME, timeout=10)
            try:
                conn.execute("""
                    INSERT INTO offline_queue 
                    (day_date, start_time, end_time, leave_type, ojti_hours, cic_hours, timestamp) 
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (txt_date.value, s_val, e_val, leave_val, ojti, cic, datetime.now().isoformat()))
                conn.commit()
            finally:
                conn.close()

            # Refresh views
            load_pending_queue()
            load_paystub_summary() # This applies the pending bump to the Next Check label
            
            bump = calculate_pending_bump()
            if bump > 0:
                lbl_status.value = f"Saved! Est bump: +${bump:,.2f} Gross"
            else:
                lbl_status.value = f"Saved {txt_date.value}"
            lbl_status.color = "green"
            
        except Exception as err:
            lbl_status.value = f"Error: {str(err)}"
            lbl_status.color = "red"
        page.update()

    def sync_data_click(e):
        lbl_status.value = "Syncing with PC..."
        page.update()
        try:
            conn = sqlite3.connect(DB_NAME, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                # 1. PUSH local changes first
                rows = conn.execute("SELECT * FROM offline_queue").fetchall()
                if rows:
                    payload = [dict(r) for r in rows]
                    r = requests.post(f"{get_url()}/mobile_sync", json=payload, timeout=5)
                    if r.status_code == 200:
                        conn.execute("DELETE FROM offline_queue")
                        conn.commit()
                
                # 2. PULL fresh data
                r_sched = requests.get(f"{get_url()}/get_schedule_defaults", timeout=5)
                r_shifts = requests.get(f"{get_url()}/get_saved_shifts?year={datetime.now().year}", timeout=5)
                r_summary = requests.get(f"{get_url()}/get_paystubs_summary", timeout=5)
                
                # Fetch YTD for all relevant years (2023 to current year)
                ytd_data_by_year = {}
                for y in range(2023, datetime.now().year + 1):
                    r_ytd = requests.get(f"{get_url()}/get_ytd_stats?year={y}", timeout=5)
                    if r_ytd.status_code == 200:
                        ytd_data_by_year[y] = r_ytd.json()
                
                holidays = []
                years = [datetime.now().year, datetime.now().year + 1]
                for y in years:
                    r_hol = requests.get(f"{get_url()}/get_holidays?year={y}", timeout=5)
                    if r_hol.status_code == 200:
                        holidays.extend(r_hol.json())

                # 3. WRITE downloaded data
                if r_sched.status_code == 200:
                    conn.execute("DELETE FROM schedule_defaults")
                    for i in r_sched.json():
                        conn.execute("INSERT INTO schedule_defaults VALUES (?,?,?,?)", 
                                     (i['year'], i['day'], i['start'], i['end']))
                
                if r_shifts.status_code == 200:
                    conn.execute("DELETE FROM server_actuals")
                    for s in r_shifts.json():
                        conn.execute("""
                            INSERT INTO server_actuals (day_date, start_time, end_time, leave_type, ojti_hours, cic_hours)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (s['date'], s['start'], s['end'], s['leave'], s['ojti'], s['cic']))

                if holidays:
                    conn.execute("DELETE FROM holiday_cache")
                    for h in holidays:
                        conn.execute("INSERT INTO holiday_cache VALUES (?,?,?,?)", 
                                     (h['year'], h['name'], h['date'], h['day']))

                if r_summary.status_code == 200:
                    conn.execute("DELETE FROM paystub_summary")
                    data = r_summary.json()
                    
                    # Store Base Rate for local math
                    base_rate = data.get('base_rate', 0.0)
                    conn.execute("INSERT INTO paystub_summary VALUES (?,?,?,?)", ('rate', 'base', base_rate, None))
                    
                    for h in data.get('history', []):
                        conn.execute("INSERT INTO paystub_summary VALUES (?,?,?,?)", ('history', h['period_ending'], h['net_pay'], None))
                    for l in data.get('leave', []):
                        conn.execute("INSERT INTO paystub_summary VALUES (?,?,?,?)", ('leave', l['type'], l['balance'], None))
                    proj = data.get('projected')
                    if proj:
                        conn.execute("INSERT INTO paystub_summary VALUES (?,?,?,?)", ('projected', proj['period_ending'], proj['net_pay'], None))
                        
                # Write all downloaded YTD years
                for y, data in ytd_data_by_year.items():
                    conn.execute("DELETE FROM ytd_stats WHERE year=?", (y,))
                    for e in data.get('earnings', []):
                        conn.execute("INSERT INTO ytd_stats VALUES (?,?,?,?,?)", (y, 'earnings', e['type'], e['Amount'], e['Hours']))
                    for d in data.get('deductions', []):
                        conn.execute("INSERT INTO ytd_stats VALUES (?,?,?,?,?)", (y, 'deductions', d['type'], d['Amount'], 0.0))

                conn.commit()
            finally:
                conn.close()

            lbl_status.value = "Sync Complete!"
            lbl_status.color = "green"
            
            # Reload all views
            load_holidays_from_db()
            load_paystub_summary()
            load_ytd_summary()
            load_pending_queue()
            change_date(None)
            
        except Exception as err:
            lbl_status.value = f"Sync Failed: {str(err)}"
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
            ft.ElevatedButton("Sync data with PC", icon=ft.Icons.SYNC, on_click=sync_data_click, width=400),
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
        conn = sqlite3.connect(DB_NAME, timeout=10)
        try:
            rows = conn.execute(
                "SELECT name, date, day FROM holiday_cache WHERE year >= ? ORDER BY date", 
                (datetime.now().year,)
            ).fetchall()
        finally:
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
    # TAB 3: PAYSTUB SUMMARY
    # ==========================================
    lbl_projected = ft.Text("Next Check: N/A", size=20, weight="bold", color=ft.Colors.GREEN_700)
    
    leave_table = ft.DataTable(
        columns=[ft.DataColumn(ft.Text("Leave Type")), ft.DataColumn(ft.Text("Balance"))],
        heading_row_color=ft.Colors.GREY_200,
        width=400
    )
    
    history_table = ft.DataTable(
        columns=[ft.DataColumn(ft.Text("Period Ending")), ft.DataColumn(ft.Text("Net Pay"))],
        heading_row_color=ft.Colors.GREY_200,
        width=400
    )

    def load_paystub_summary():
        conn = sqlite3.connect(DB_NAME, timeout=10)
        try:
            rows = conn.execute("SELECT type, label, value1 FROM paystub_summary").fetchall()
        finally:
            conn.close()
            
        leave_table.rows.clear()
        history_table.rows.clear()
        lbl_projected.value = "Next Check: N/A"
        
        pending_bump = calculate_pending_bump()
        
        for r_type, label, val in rows:
            if r_type == 'projected':
                if pending_bump > 0:
                    lbl_projected.value = f"Next Check ({label}): ${val:,.2f} (+${pending_bump:,.2f} pending)"
                else:
                    lbl_projected.value = f"Next Check ({label}): ${val:,.2f}"
            elif r_type == 'leave':
                leave_table.rows.append(ft.DataRow(cells=[
                    ft.DataCell(ft.Text(label)), 
                    ft.DataCell(ft.Text(f"{val:.2f}"))
                ]))
            elif r_type == 'history':
                history_table.rows.append(ft.DataRow(cells=[
                    ft.DataCell(ft.Text(label)), 
                    ft.DataCell(ft.Text(f"${val:,.2f}"))
                ]))
        page.update()

    tab_paystub_content = ft.Container(
        padding=10,
        content=ft.Column([
            ft.Text("Paystub Summary", size=20, weight="bold"),
            lbl_projected,
            ft.Divider(),
            ft.Text("Current Leave Balances", size=16, weight="bold"),
            leave_table,
            ft.Divider(),
            ft.Text("Recent History", size=16, weight="bold"),
            history_table,
        ], scroll=ft.ScrollMode.ADAPTIVE, height=650)
    )

    # ==========================================
    # TAB 4: YTD STATS
    # ==========================================
    ytd_earn_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("Earnings Type")), 
            ft.DataColumn(ft.Text("Hrs")), 
            ft.DataColumn(ft.Text("Amount"))
        ],
        heading_row_color=ft.Colors.GREY_200,
        width=400,
        column_spacing=10
    )
    
    ytd_ded_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("Deduction Type")), 
            ft.DataColumn(ft.Text("Amount"))
        ],
        heading_row_color=ft.Colors.GREY_200,
        width=400
    )

    current_year = datetime.now().year
    dd_ytd_year = ft.Dropdown(
        label="Year",
        options=[ft.dropdown.Option(str(y)) for y in range(2023, current_year + 1)],
        value=str(current_year),
        width=100,
        on_change=lambda e: load_ytd_summary()
    )

    def load_ytd_summary():
        selected_year = int(dd_ytd_year.value) if dd_ytd_year.value else datetime.now().year
        conn = sqlite3.connect(DB_NAME, timeout=10)
        try:
            rows = conn.execute("SELECT category, type, amount, hours FROM ytd_stats WHERE year=?", (selected_year,)).fetchall()
        finally:
            conn.close()
            
        ytd_earn_table.rows.clear()
        ytd_ded_table.rows.clear()
        
        earnings_dict = {}
        deductions_list = []
        
        for cat, r_type, amt, hrs in rows:
            if cat == 'earnings':
                # Combine FSLA Premium and True Overtime
                if r_type in ["FLSA Premium", "True Overtime"]:
                    r_type = "Overtime"
                
                if r_type not in earnings_dict:
                    earnings_dict[r_type] = {"amount": 0.0, "hours": 0.0}
                earnings_dict[r_type]["amount"] += amt
                earnings_dict[r_type]["hours"] += hrs
            elif cat == 'deductions':
                deductions_list.append((r_type, amt))
                
        for r_type, data in earnings_dict.items():
            ytd_earn_table.rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(r_type, size=12)), 
                ft.DataCell(ft.Text(f"{data['hours']:.2f}", size=12)), 
                ft.DataCell(ft.Text(f"${data['amount']:,.2f}", size=12))
            ]))
            
        for r_type, amt in deductions_list:
            ytd_ded_table.rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(r_type, size=12)), 
                ft.DataCell(ft.Text(f"${amt:,.2f}", size=12))
            ]))
            
        page.update()

    tab_ytd_content = ft.Container(
        padding=10,
        content=ft.Column([
            ft.Row([ft.Text("YTD Totals", size=20, weight="bold"), dd_ytd_year], alignment="spaceBetween"),
            ft.Divider(),
            ft.Text("Earnings", size=16, weight="bold"),
            ytd_earn_table,
            ft.Divider(),
            ft.Text("Deductions", size=16, weight="bold"),
            ytd_ded_table,
        ], scroll=ft.ScrollMode.ADAPTIVE, height=650)
    )

    # ==========================================
    # TAB 5: PENDING (RAW DUMP)
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
        conn = sqlite3.connect(DB_NAME, timeout=10)
        try:
            rows = conn.execute("SELECT day_date, start_time, end_time, leave_type, ojti_hours, cic_hours FROM offline_queue ORDER BY day_date DESC").fetchall()
        finally:
            conn.close()

        pending_table.rows.clear()
        
        def fmt_hours(val):
            if not val or val <= 0: return "-"
            h = int(val)
            m = int(round((val - h) * 60))
            return f"{h}:{m:02d}"

        for d, s, e, l, o, c in rows:
            s_disp = s if s else "-"
            e_disp = e if e else "-"
            l_disp = l if l and l != "None" else "-"
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
            ft.Tab(text="PayStub", icon=ft.Icons.ATTACH_MONEY, content=tab_paystub_content),
            ft.Tab(text="YTD", icon=ft.Icons.BAR_CHART, content=tab_ytd_content),
            ft.Tab(text="Holidays", icon=ft.Icons.CALENDAR_MONTH, content=tab_holidays_content),
            ft.Tab(text="Pending", icon=ft.Icons.PENDING_ACTIONS, content=tab_pending_content),
        ],
        expand=1,
    )

    page.add(t)
    
    # Load all cached views
    load_holidays_from_db()
    load_paystub_summary()
    load_ytd_summary()
    load_pending_queue()
    change_date(None)

if __name__ == "__main__":
    ft.app(target=main)
