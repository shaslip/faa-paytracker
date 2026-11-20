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
        c.execute('''
            CREATE TABLE IF NOT EXISTS available_years (
                year INTEGER PRIMARY KEY
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS paystub_details (
                period_ending TEXT PRIMARY KEY,
                data_json TEXT
            )
        ''')
        conn.commit()
    finally:
        conn.close()

def main(page: ft.Page):
    APP_VERSION = "1.3.9"
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
    # LOCAL MATH ENGINE (Estimates Net Bump & Leave)
    # ==========================================
    REF_DATE = datetime(2024, 12, 14).date()

    def get_current_pay_period():
        today = datetime.now().date()
        diff = (today - REF_DATE).days
        remainder = diff % 14
        curr_pe = today if remainder == 0 else today + timedelta(days=(14 - remainder))
        curr_start = curr_pe - timedelta(days=13)
        return curr_start, curr_pe

    def hmm_to_min(v):
        if v is None: return 0
        sign = -1 if v < 0 else 1
        av = abs(float(v))
        h = int(av)
        m = round((av - h) * 100)
        return sign * ((h * 60) + m)

    def min_to_hmm(m):
        sign = -1 if m < 0 else 1
        am = abs(int(round(m)))
        return sign * ((am // 60) + ((am % 60) / 100.0))

    def map_leave_balance_key(bal_type):
        if bal_type == "Annual Leave": return "Annual"
        if bal_type == "Sick Leave": return "Sick"
        if bal_type in ("Credit", "Credit Hours"): return "Credit"
        if bal_type in ("Comp", "Comp Time"): return "Comp"
        return None

    def calculate_pending_leave_used():
        """Returns {leave_key: minutes_used} for shifts after the last official paystub."""
        conn = sqlite3.connect(DB_NAME, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            pe_row = conn.execute("SELECT label FROM paystub_summary WHERE type='last_pe'").fetchone()
            last_pe = pe_row['label'] if pe_row else None
            if not last_pe:
                hist_row = conn.execute(
                    "SELECT label FROM paystub_summary WHERE type='history' ORDER BY label DESC LIMIT 1"
                ).fetchone()
                last_pe = hist_row['label'] if hist_row else None

            # Merge server_actuals and offline_queue (offline_queue overrides per day_date)
            merged = {}
            for r in conn.execute("SELECT day_date, start_time, end_time, leave_type FROM server_actuals").fetchall():
                merged[r['day_date']] = dict(r)
            for r in conn.execute("SELECT day_date, start_time, end_time, leave_type FROM offline_queue ORDER BY id ASC").fetchall():
                merged[r['day_date']] = dict(r)

            def time_diff_hrs(start_str, end_str):
                if not start_str or not end_str: return 0.0
                s = datetime.strptime(start_str, "%H:%M")
                e = datetime.strptime(end_str, "%H:%M")
                if e < s: e += timedelta(days=1)
                return (e - s).total_seconds() / 3600.0

            used_mins = {}
            for date_str, entry in merged.items():
                if last_pe and date_str <= last_pe:
                    continue
                l_type = entry.get('leave_type')
                if not l_type or l_type in ["None", "", "Holiday", "LWOP"]:
                    continue

                dt = datetime.strptime(date_str, "%Y-%m-%d")
                def_row = conn.execute(
                    "SELECT start_time, end_time FROM schedule_defaults WHERE year=? AND day_idx=?",
                    (dt.year, dt.weekday())
                ).fetchone()
                if not def_row or not def_row['start_time'] or not def_row['end_time']:
                    continue

                std_hours = time_diff_hrs(def_row['start_time'], def_row['end_time'])
                s_str = entry.get('start_time') or ""
                e_str = entry.get('end_time') or ""
                worked_hours = time_diff_hrs(s_str, e_str) if (s_str and e_str) else 0.0
                leave_hours = max(0.0, std_hours - worked_hours)
                if leave_hours > 0:
                    used_mins[l_type] = used_mins.get(l_type, 0) + int(round(leave_hours * 60))

            return used_mins
        finally:
            conn.close()

    def calculate_pending_bump():
        conn = sqlite3.connect(DB_NAME, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rate_row = conn.execute("SELECT value1 FROM paystub_summary WHERE type='rate'").fetchone()
            base_rate = rate_row['value1'] if rate_row else 0.0
            if base_rate <= 0: return 0.0

            ratio_row = conn.execute("SELECT value1 FROM paystub_summary WHERE type='net_ratio'").fetchone()
            net_ratio = ratio_row['value1'] if ratio_row else 0.70

            proj_row = conn.execute("SELECT label FROM paystub_summary WHERE type='projected'").fetchone()
            next_pe = proj_row['label'] if proj_row else None

            queue = conn.execute("SELECT * FROM offline_queue ORDER BY id ASC").fetchall()
            if not queue: return 0.0

            # Deduplicate by day_date (keep latest entry per date)
            latest_queue = {q['day_date']: q for q in queue}

            # Load cached observed holidays
            hol_rows = conn.execute("SELECT date FROM holiday_cache").fetchall()
            holidays = set(r['date'] for r in hol_rows)

            def time_diff(start_str, end_str):
                if not start_str or not end_str: return 0.0
                s = datetime.strptime(start_str, "%H:%M")
                e = datetime.strptime(end_str, "%H:%M")
                if e < s: e += timedelta(days=1)
                return (e - s).total_seconds() / 3600.0

            def day_extra_gross(s_str, e_str, leave_str, ojti_val, cic_val, std_hours, is_hol):
                # If taking full-day leave with no start/end times entered, hours worked is 0
                act_hours = 0.0 if (leave_str and leave_str not in ["None", ""] and not s_str) else time_diff(s_str, e_str)
                ot_hours = max(0.0, act_hours - std_hours)
                hol_worked = min(8.0, act_hours) if (is_hol and act_hours > 0 and leave_str != "Holiday") else 0.0
                
                g = (ot_hours * base_rate * 1.5)
                g += (hol_worked * base_rate)
                g += ((ojti_val or 0.0) * base_rate * 0.25)
                g += ((cic_val or 0.0) * base_rate * 0.10)
                return g

            gross_diff = 0.0

            for date_str, q in latest_queue.items():
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                
                # Only apply to the upcoming projected check's 14-day period
                if next_pe:
                    pe_dt = datetime.strptime(next_pe, "%Y-%m-%d")
                    start_dt = pe_dt - timedelta(days=13)
                    if not (start_dt <= dt <= pe_dt):
                        continue

                day_idx = dt.weekday()
                target_year = dt.year
                is_hol = date_str in holidays

                def_row = conn.execute("SELECT start_time, end_time FROM schedule_defaults WHERE year=? AND day_idx=?", (target_year, day_idx)).fetchone()
                act_row = conn.execute("SELECT * FROM server_actuals WHERE day_date=?", (date_str,)).fetchone()

                std_hours = 0.0
                if def_row and def_row['start_time'] and def_row['end_time']:
                    std_hours = time_diff(def_row['start_time'], def_row['end_time'])

                # 1. Baseline already included in server's projected net_pay
                if act_row:
                    base_gross = day_extra_gross(act_row['start_time'], act_row['end_time'], act_row['leave_type'], act_row['ojti_hours'], act_row['cic_hours'], std_hours, is_hol)
                elif def_row and def_row['start_time'] and def_row['end_time']:
                    base_gross = day_extra_gross(def_row['start_time'], def_row['end_time'], None, 0.0, 0.0, std_hours, is_hol)
                else:
                    base_gross = 0.0

                # 2. New value from local entry
                new_gross = day_extra_gross(q['start_time'], q['end_time'], q['leave_type'], q['ojti_hours'], q['cic_hours'], std_hours, is_hol)

                gross_diff += (new_gross - base_gross)

            return gross_diff * net_ratio
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

    def on_leave_change(e):
        try:
            dt = datetime.strptime(txt_date.value, "%Y-%m-%d")
        except Exception:
            return
        conn = sqlite3.connect(DB_NAME, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            row_def = conn.execute(
                "SELECT start_time, end_time FROM schedule_defaults WHERE year=? AND day_idx=?",
                (dt.year, dt.weekday())
            ).fetchone()
        finally:
            conn.close()

        def_start = (row_def['start_time'] or "") if row_def else ""
        def_end = (row_def['end_time'] or "") if row_def else ""
        cur_start = (txt_start.value or "").strip()
        cur_end = (txt_end.value or "").strip()

        if dd_leave.value and dd_leave.value != "None":
            # Auto-clear Start/End if they still match the standard default shift
            if def_start and def_end and cur_start == def_start and cur_end == def_end:
                txt_start.value = ""
                txt_end.value = ""
                txt_start.data = 0
                txt_end.data = 0
                page.update()
        else:
            # Restore standard shift if cleared and user switched leave back to None
            if not cur_start and not cur_end and def_start and def_end:
                txt_start.value = def_start
                txt_end.value = def_end
                txt_start.data = len(def_start)
                txt_end.data = len(def_end)
                page.update()

    dd_leave = ft.Dropdown(
        label="Leave Type (Optional)",
        options=[
            ft.dropdown.Option("None"), ft.dropdown.Option("Annual"),
            ft.dropdown.Option("Sick"), ft.dropdown.Option("Holiday"),
            ft.dropdown.Option("Credit"), ft.dropdown.Option("Comp"),
            ft.dropdown.Option("LWOP"),
        ],
        value="None",
        on_change=on_leave_change,
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
                conn.execute("DELETE FROM offline_queue WHERE day_date=?", (txt_date.value,))
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
            if abs(bump) > 0.01:
                sign = "+" if bump > 0 else ""
                lbl_status.value = f"Saved! Est check change: {sign}${bump:,.2f} Net"
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

                # Incremental pull of full paystub details for offline viewing
                known_rows = conn.execute("SELECT period_ending FROM paystub_details").fetchall()
                known_periods = [r['period_ending'] for r in known_rows]
                r_details = None
                try:
                    r_details = requests.post(
                        f"{get_url()}/get_paystubs_detail",
                        json={"known_periods": known_periods},
                        timeout=20
                    )
                except Exception:
                    r_details = None
                
                # --- NEW: Fetch available years ---
                r_years = requests.get(f"{get_url()}/get_available_years", timeout=5)
                available_years = [datetime.now().year]
                if r_years.status_code == 200:
                    available_years = r_years.json()
                
                # Fetch YTD for all dynamic years
                ytd_data_by_year = {}
                for y in available_years:
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
                
                # --- Save available years ---
                if r_years.status_code == 200:
                    conn.execute("DELETE FROM available_years")
                    for y in available_years:
                        conn.execute("INSERT INTO available_years VALUES (?)", (y,))
                        
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
                    
                    # Store Base Rate & Net Ratio for local math
                    base_rate = data.get('base_rate', 0.0)
                    net_ratio = data.get('net_ratio', 0.70)
                    conn.execute("INSERT INTO paystub_summary VALUES (?,?,?,?)", ('rate', 'base', base_rate, None))
                    conn.execute("INSERT INTO paystub_summary VALUES (?,?,?,?)", ('net_ratio', 'ratio', net_ratio, None))
                    last_pe = data.get('last_period_ending')
                    if last_pe:
                        conn.execute("INSERT INTO paystub_summary VALUES (?,?,?,?)", ('last_pe', last_pe, 0.0, None))
                    
                    for h in data.get('history', []):
                        stub_id_str = str(h['id']) if 'id' in h and h['id'] is not None else None
                        conn.execute("INSERT INTO paystub_summary VALUES (?,?,?,?)", ('history', h['period_ending'], h['net_pay'], stub_id_str))
                    for l in data.get('leave', []):
                        conn.execute("INSERT INTO paystub_summary VALUES (?,?,?,?)", ('leave', l['type'], l['balance'], None))
                    proj = data.get('projected')
                    if proj:
                        conn.execute("INSERT INTO paystub_summary VALUES (?,?,?,?)", ('projected', proj['period_ending'], proj['net_pay'], None))

                if r_details is not None and r_details.status_code == 200:
                    for pe, stub_obj in r_details.json().items():
                        conn.execute(
                            "INSERT OR REPLACE INTO paystub_details (period_ending, data_json) VALUES (?, ?)",
                            (pe, json.dumps(stub_obj))
                        )
                        
                # Write all downloaded YTD years
                for y, data in ytd_data_by_year.items():
                    conn.execute("DELETE FROM ytd_stats WHERE year=?", (y,))
                    pp_cnt = data.get('pay_periods', 0)
                    if pp_cnt:
                        conn.execute("INSERT INTO ytd_stats VALUES (?,?,?,?,?)", (y, 'meta', 'pay_periods', float(pp_cnt), 0.0))
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
    lbl_curr_pp = ft.Text("", size=12, color=ft.Colors.GREY_600)

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

        curr_start, curr_pe = get_current_pay_period()
        lbl_curr_pp.value = f"Current Pay Period: {curr_start.strftime('%Y-%m-%d')} to {curr_pe.strftime('%Y-%m-%d')}"

        holiday_table.rows.clear()
        for name, date_str, day in rows:
            in_curr_pp = False
            try:
                h_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                in_curr_pp = (curr_start <= h_date <= curr_pe)
            except Exception:
                in_curr_pp = False

            holiday_table.rows.append(
                ft.DataRow(
                    color=ft.Colors.AMBER_100 if in_curr_pp else None,
                    cells=[
                        ft.DataCell(ft.Text(name, size=12, weight="bold" if in_curr_pp else None)),
                        ft.DataCell(ft.Text(date_str, weight="bold")),
                        ft.DataCell(ft.Text(day, size=12, weight="bold" if in_curr_pp else None)),
                    ]
                )
            )
        page.update()

    tab_holidays_content = ft.Container(
        padding=10,
        content=ft.Column([
            ft.Text("My Observed Holidays", size=20, weight="bold"),
            lbl_curr_pp,
            ft.Divider(),
            ft.Column([holiday_table], scroll=ft.ScrollMode.ADAPTIVE, height=600)
        ])
    )

    # ==========================================
    # TAB 3: PAYSTUB SUMMARY
    # ==========================================
    lbl_projected = ft.Text("Next Check: N/A", size=20, weight="bold", color=ft.Colors.GREEN_700)
    history_limit = 5
    
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

    paystub_dialog_content = ft.Column([], scroll=ft.ScrollMode.ADAPTIVE, spacing=8)
    paystub_dialog = ft.AlertDialog(
        title=ft.Text("Paystub Details"),
        inset_padding=ft.padding.symmetric(horizontal=10, vertical=16),
        title_padding=ft.padding.only(left=12, right=12, top=12, bottom=4),
        content_padding=ft.padding.symmetric(horizontal=10, vertical=6),
        content=ft.Container(content=paystub_dialog_content, width=500, height=580),
        actions=[
            ft.TextButton("Close", on_click=lambda e: setattr(paystub_dialog, 'open', False) or page.update())
        ],
    )
    page.overlay.append(paystub_dialog)

    def _fmt_money(v):
        if v is None: return "0.00"
        try:
            return f"{float(v):,.2f}"
        except Exception:
            return str(v)

    def _fmt_num(v):
        if v is None: return "0.00"
        try:
            return f"{float(v):.2f}"
        except Exception:
            return str(v)

    def _section_header(title):
        return ft.Container(
            content=ft.Text(title, size=13, weight="bold", color=ft.Colors.WHITE),
            bgcolor=ft.Colors.BLUE_800,
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            border_radius=4,
        )

    def _load_paystub_detail(period_ending):
        conn = sqlite3.connect(DB_NAME, timeout=10)
        try:
            row = conn.execute(
                "SELECT data_json FROM paystub_details WHERE period_ending = ?",
                (period_ending,)
            ).fetchone()
            if row:
                return json.loads(row[0])
        finally:
            conn.close()

        # Live fallback if not cached locally
        try:
            r = requests.get(
                f"{get_url()}/get_paystub_detail",
                params={"period_ending": period_ending},
                timeout=5
            )
            if r.status_code == 200:
                data = r.json()
                conn = sqlite3.connect(DB_NAME, timeout=10)
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO paystub_details (period_ending, data_json) VALUES (?, ?)",
                        (period_ending, json.dumps(data))
                    )
                    conn.commit()
                finally:
                    conn.close()
                return data
        except Exception:
            return None
        return None

    def _populate_paystub_tables(earnings, deductions, leave, flags, remarks_raw):
        def num_cell(txt, w, size=10, bold=False, color=None):
            return ft.Container(
                width=w,
                alignment=ft.alignment.center_right,
                content=ft.Text(
                    txt,
                    size=size,
                    weight="bold" if bold else None,
                    color=color,
                    no_wrap=True,
                    max_lines=1,
                ),
            )

        if earnings:
            paystub_dialog_content.controls.append(_section_header("Earnings"))
            paystub_dialog_content.controls.append(
                ft.Row([
                    ft.Text("Type", size=10, weight="bold", expand=True),
                    num_cell("Rate", 50, bold=True),
                    num_cell("Hrs", 44, bold=True),
                    num_cell("Current", 72, bold=True),
                ], spacing=4)
            )
            for r in earnings:
                paystub_dialog_content.controls.append(
                    ft.Row([
                        ft.Text(str(r.get("type", "")), size=11, expand=True),
                        num_cell(_fmt_num(r.get("rate")), 50),
                        num_cell(_fmt_num(r.get("hours_current")), 44),
                        num_cell(_fmt_money(r.get("amount_current")), 72),
                    ], spacing=4)
                )

        if deductions:
            paystub_dialog_content.controls.append(_section_header("Deductions"))
            paystub_dialog_content.controls.append(
                ft.Row([
                    ft.Text("Type", size=10, weight="bold", expand=True),
                    num_cell("Current", 70, bold=True),
                    num_cell("YTD", 78, bold=True),
                ], spacing=4)
            )
            for r in deductions:
                paystub_dialog_content.controls.append(
                    ft.Row([
                        ft.Text(str(r.get("type", "")), size=11, expand=True),
                        num_cell(_fmt_money(r.get("amount_current")), 70),
                        num_cell(_fmt_money(r.get("amount_ytd")), 78),
                    ], spacing=4)
                )

        if leave:
            paystub_dialog_content.controls.append(_section_header("Leave"))
            paystub_dialog_content.controls.append(
                ft.Row([
                    ft.Text("Type", size=10, weight="bold", expand=True),
                    num_cell("Start", 42, bold=True),
                    num_cell("Earn", 38, bold=True),
                    num_cell("Used", 40, bold=True),
                    num_cell("Adv", 38, bold=True),
                    num_cell("End", 44, bold=True),
                ], spacing=3)
            )
            for r in leave:
                l_type = str(r.get("type", ""))
                fid = f"leave_{l_type}_end"
                is_err = fid in flags
                adv_val = float(r.get("advanced") or 0.0)
                end_val = float(r.get("balance_end") or 0.0)
                adv_err = is_err and adv_val > 0 and end_val == 0
                end_err = is_err and not adv_err
                paystub_dialog_content.controls.append(
                    ft.Row([
                        ft.Text(l_type, size=10, expand=True),
                        num_cell(_fmt_num(r.get("balance_start")), 42),
                        num_cell(_fmt_num(r.get("earned_current")), 38),
                        num_cell(_fmt_num(r.get("used_current")), 40),
                        num_cell(_fmt_num(adv_val), 38, bold=adv_err, color=ft.Colors.RED_700 if adv_err else None),
                        num_cell(_fmt_num(end_val), 44, bold=end_err, color=ft.Colors.RED_700 if end_err else None),
                    ], spacing=3)
                )

        if flags:
            flag_lines = [ft.Text(f"• {msg}", size=11, color=ft.Colors.RED_800) for msg in flags.values()]
            paystub_dialog_content.controls.append(
                ft.Container(
                    bgcolor=ft.Colors.RED_50,
                    border=ft.border.all(1, ft.Colors.RED_200),
                    border_radius=4,
                    padding=6,
                    content=ft.Column([ft.Text("Audit Flags", size=11, weight="bold", color=ft.Colors.RED_800)] + flag_lines, spacing=2),
                )
            )

        remarks = (remarks_raw or "").strip()
        if remarks:
            paystub_dialog_content.controls.append(_section_header("Remarks"))
            paystub_dialog_content.controls.append(
                ft.Container(
                    bgcolor=ft.Colors.GREY_100,
                    border=ft.border.all(1, ft.Colors.GREY_300),
                    border_radius=4,
                    padding=6,
                    content=ft.Text(remarks, size=10, font_family="monospace"),
                )
            )

    def open_paystub_dialog(period_ending):
        data = _load_paystub_detail(period_ending)
        paystub_dialog_content.controls.clear()
        paystub_dialog.title = ft.Text(f"Paystub: {period_ending}", size=16, weight="bold")

        if not data:
            paystub_dialog_content.controls.append(
                ft.Text(
                    "Paystub details are not cached yet.\nTap 'Sync data with PC' to download full paystubs for offline viewing.",
                    size=13,
                    color=ft.Colors.GREY_700,
                )
            )
            paystub_dialog.open = True
            page.update()
            return

        stub = data.get("stub", {})
        earnings = data.get("earnings", [])
        deductions = data.get("deductions", [])
        leave = data.get("leave", [])
        flags = data.get("flags", {}) or {}

        # Header Card (Employee Express Replica)
        paystub_dialog_content.controls.append(
            ft.Container(
                bgcolor=ft.Colors.BLUE_50,
                border=ft.border.all(1, ft.Colors.BLUE_200),
                border_radius=6,
                padding=8,
                content=ft.Column([
                    ft.Text(stub.get("agency") or "Federal Aviation Administration", size=13, weight="bold"),
                    ft.Text("Earnings and Leave Statement", size=11, color=ft.Colors.GREY_700),
                    ft.Divider(height=6),
                    ft.Row([
                        ft.Column([
                            ft.Text("Period Ending", size=10, color=ft.Colors.GREY_600),
                            ft.Text(str(stub.get("period_ending", "")), size=12, weight="bold"),
                        ], spacing=1),
                        ft.Column([
                            ft.Text("Pay Date", size=10, color=ft.Colors.GREY_600),
                            ft.Text(str(stub.get("pay_date", "")), size=12, weight="bold"),
                        ], spacing=1),
                        ft.Column([
                            ft.Text("Net Pay", size=10, color=ft.Colors.BLUE_800, weight="bold"),
                            ft.Text(
                                f"${_fmt_money(stub.get('net_pay'))}",
                                size=13,
                                weight="bold",
                                color=ft.Colors.RED_700 if "net_pay" in flags else ft.Colors.GREEN_800,
                            ),
                        ], spacing=1, horizontal_alignment=ft.CrossAxisAlignment.END),
                    ], alignment="spaceBetween"),
                ], spacing=2),
            )
        )

        # Pay Summary
        paystub_dialog_content.controls.append(_section_header("Your Pay Consists of"))
        for s_lbl, s_val, is_err in [
            ("Gross Pay", _fmt_money(stub.get("gross_pay")), "gross_pay" in flags),
            ("Total Deductions", _fmt_money(stub.get("total_deductions")), False),
            ("Net Pay", _fmt_money(stub.get("net_pay")), "net_pay" in flags),
        ]:
            paystub_dialog_content.controls.append(
                ft.Row([
                    ft.Text(s_lbl, size=12, weight="bold" if s_lbl == "Net Pay" else None),
                    ft.Text(
                        f"${s_val}",
                        size=12,
                        weight="bold" if (s_lbl == "Net Pay" or is_err) else None,
                        color=ft.Colors.RED_700 if is_err else None,
                    ),
                ], alignment="spaceBetween")
            )

        _populate_paystub_tables(earnings, deductions, leave, flags, stub.get("remarks"))
        paystub_dialog.open = True
        page.update()

    def show_more_history(e):
        nonlocal history_limit
        history_limit += 5
        load_paystub_summary()

    btn_show_more = ft.OutlinedButton(
        "Show More",
        on_click=show_more_history,
        visible=False,
        width=400,
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
        pending_leave_mins = calculate_pending_leave_used()
        history_rows = []

        for r_type, label, val in rows:
            if r_type == 'projected':
                est_net = val + pending_bump
                lbl_projected.value = f"Next Check ({label}): ${est_net:,.2f}"
            elif r_type == 'leave':
                l_key = map_leave_balance_key(label)
                deduct_mins = pending_leave_mins.get(l_key, 0) if l_key else 0
                if deduct_mins > 0:
                    adj_min = hmm_to_min(val) - deduct_mins
                    adj_val = min_to_hmm(adj_min)
                    deduct_dot = min_to_hmm(deduct_mins)
                    bal_text = f"{adj_val:.2f} (-{deduct_dot:.2f})"
                else:
                    bal_text = f"{val:.2f}"
                leave_table.rows.append(ft.DataRow(cells=[
                    ft.DataCell(ft.Text(label)),
                    ft.DataCell(ft.Text(
                        bal_text,
                        color=ft.Colors.ORANGE_800 if deduct_mins > 0 else None,
                        weight="bold" if deduct_mins > 0 else None
                    ))
                ]))
            elif r_type == 'history':
                history_rows.append((label, val))

        history_rows.sort(key=lambda x: x[0], reverse=True)
        visible_history = history_rows[:history_limit]

        for label, val in visible_history:
            history_table.rows.append(ft.DataRow(cells=[
                ft.DataCell(
                    ft.Container(
                        content=ft.Text(
                            label,
                            color=ft.Colors.BLUE_700,
                            weight="bold",
                            style=ft.TextStyle(decoration=ft.TextDecoration.UNDERLINE),
                        ),
                        on_click=lambda e, pe=label: open_paystub_dialog(pe),
                    )
                ),
                ft.DataCell(ft.Text(f"${val:,.2f}"))
            ]))

        if len(history_rows) > history_limit:
            btn_show_more.text = f"Show More ({len(visible_history)} of {len(history_rows)})"
            btn_show_more.visible = True
        else:
            btn_show_more.visible = False

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
            btn_show_more,
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

    ytd_proj_earn_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("Earnings Type")),
            ft.DataColumn(ft.Text("Hrs")),
            ft.DataColumn(ft.Text("Amount"))
        ],
        heading_row_color=ft.Colors.BLUE_50,
        width=400,
        column_spacing=10
    )

    lbl_ytd_proj_sub = ft.Text("", size=11, color=ft.Colors.GREY_600)
    
    ytd_ded_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("Deduction Type")), 
            ft.DataColumn(ft.Text("Amount"))
        ],
        heading_row_color=ft.Colors.GREY_200,
        width=400
    )

   # Remove the hardcoded options; they will be populated dynamically
    dd_ytd_year = ft.Dropdown(
        label="Year",
        expand=True,
        on_change=lambda e: load_ytd_summary()
    )

    def load_ytd_summary():
        conn = sqlite3.connect(DB_NAME, timeout=10)
        try:
            # 1. Dynamically populate the Dropdown based on synced data
            years_rows = conn.execute("SELECT year FROM available_years ORDER BY year DESC").fetchall()
            avail_years = [r[0] for r in years_rows]
            
            # Fallback if app hasn't synced yet
            if not avail_years:
                avail_years = [datetime.now().year]
                
            dd_ytd_year.options = [ft.dropdown.Option(str(y)) for y in avail_years]
            
            # Auto-select the newest year if nothing is selected yet
            if not dd_ytd_year.value or int(dd_ytd_year.value) not in avail_years:
                dd_ytd_year.value = str(avail_years[0])
                
            selected_year = int(dd_ytd_year.value)
            
            # 2. Load the actual data for the selected year
            rows = conn.execute("SELECT category, type, amount, hours FROM ytd_stats WHERE year=?", (selected_year,)).fetchall()
            detail_rows = conn.execute("SELECT data_json FROM paystub_details").fetchall()
        finally:
            conn.close()
            
        ytd_earn_table.rows.clear()
        ytd_proj_earn_table.rows.clear()
        ytd_ded_table.rows.clear()
        
        earnings_dict = {}
        deductions_list = []
        pp_count = 0
        
        for cat, r_type, amt, hrs in rows:
            if cat == 'meta' and r_type == 'pay_periods':
                pp_count = int(round(amt))
            elif cat == 'earnings':
                # Combine FSLA Premium and True Overtime
                if r_type in ["FLSA Premium", "True Overtime"]:
                    r_type = "Overtime"
                
                if r_type not in earnings_dict:
                    earnings_dict[r_type] = {"amount": 0.0, "hours": 0.0}
                
                earnings_dict[r_type]["amount"] += amt
                
                # Prevent double-counting FLSA and True Overtime hours
                if r_type == "Overtime":
                    earnings_dict[r_type]["hours"] = max(earnings_dict[r_type]["hours"], hrs)
                else:
                    earnings_dict[r_type]["hours"] += hrs
                    
            elif cat == 'deductions':
                deductions_list.append((r_type, amt))

        if pp_count <= 0 and detail_rows:
            year_prefix = f"{selected_year}-"
            for (dj,) in detail_rows:
                try:
                    pd_str = json.loads(dj).get("stub", {}).get("pay_date") or ""
                    if pd_str.startswith(year_prefix):
                        pp_count += 1
                except Exception:
                    pass

        total_actual_amt = 0.0
        for r_type, data in earnings_dict.items():
            total_actual_amt += data['amount']
            ytd_earn_table.rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(r_type, size=12)), 
                ft.DataCell(ft.Text(f"{data['hours']:.2f}", size=12, no_wrap=True)), 
                ft.DataCell(ft.Text(f"${data['amount']:,.2f}", size=12, no_wrap=True))
            ]))
        if earnings_dict:
            ytd_earn_table.rows.append(ft.DataRow(
                color=ft.Colors.GREY_100,
                cells=[
                    ft.DataCell(ft.Text("Total", size=12, weight="bold")),
                    ft.DataCell(ft.Text("-", size=12)),
                    ft.DataCell(ft.Text(f"${total_actual_amt:,.2f}", size=12, weight="bold", no_wrap=True)),
                ]
            ))

        target_pps = max(26, pp_count) if pp_count > 0 else 26
        scale = (target_pps / pp_count) if pp_count > 0 else 0.0
        if pp_count > 0 and pp_count < 26:
            lbl_ytd_proj_sub.value = f"Projected to 26 pay periods (based on {pp_count} to date)"
        elif pp_count >= 26:
            lbl_ytd_proj_sub.value = f"Full year complete ({pp_count} pay periods)"
        else:
            lbl_ytd_proj_sub.value = "Sync with PC to calculate projected totals"

        total_proj_amt = 0.0
        if scale > 0:
            for r_type, data in earnings_dict.items():
                proj_hrs = data['hours'] * scale
                proj_amt = data['amount'] * scale
                total_proj_amt += proj_amt
                ytd_proj_earn_table.rows.append(ft.DataRow(cells=[
                    ft.DataCell(ft.Text(r_type, size=12)),
                    ft.DataCell(ft.Text(f"{proj_hrs:.2f}", size=12, no_wrap=True)),
                    ft.DataCell(ft.Text(f"${proj_amt:,.2f}", size=12, no_wrap=True)),
                ]))
            if earnings_dict:
                ytd_proj_earn_table.rows.append(ft.DataRow(
                    color=ft.Colors.BLUE_50,
                    cells=[
                        ft.DataCell(ft.Text("Total", size=12, weight="bold")),
                        ft.DataCell(ft.Text("-", size=12)),
                        ft.DataCell(ft.Text(f"${total_proj_amt:,.2f}", size=12, weight="bold", no_wrap=True)),
                    ]
                ))
            
        for r_type, amt in deductions_list:
            ytd_ded_table.rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(r_type, size=12)), 
                ft.DataCell(ft.Text(f"${amt:,.2f}", size=12, no_wrap=True))
            ]))
            
        page.update()

    tab_ytd_content = ft.Container(
        padding=10,
        content=ft.Column([
            ft.Row([ft.Text("YTD Totals", size=20, weight="bold"), dd_ytd_year]),
            ft.Divider(),
            ft.Text("Earnings (Actual)", size=16, weight="bold"),
            ytd_earn_table,
            ft.Divider(),
            ft.Text("Earnings (Projected)", size=16, weight="bold"),
            lbl_ytd_proj_sub,
            ytd_proj_earn_table,
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
