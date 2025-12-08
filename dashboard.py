import streamlit as st
import sqlite3
import pandas as pd
import os
from datetime import datetime, timedelta

# --- Configuration ---
DB_NAME = 'payroll_audit.db'
CSS_FILE = 'style.css'

st.set_page_config(page_title="FAA PayTracker", layout="wide")

# --- Load External CSS ---
def local_css(file_name):
    try:
        with open(file_name) as f:
            st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)
            # Add overrides for the "Error" text and Container width
            st.markdown("""
            <style>
                .audit-error {
                    color: red !important;
                    font-weight: bold !important;
                    text-decoration: underline wavy red !important;
                    cursor: help !important;
                }
                .stub-wrapper { max-width: 1000px; margin: 0 auto; background: white; padding: 20px; }
            </style>
            """, unsafe_allow_html=True)
    except FileNotFoundError:
        st.warning(f"Could not find {CSS_FILE}. Please create it.")

local_css(CSS_FILE)

# --- Dropdown menu helper ---
@st.cache_data
def get_audit_status_map(stub_ids):
    """
    Runs a quick audit on ALL stubs to generate status icons for the dropdown.
    Cached so it doesn't slow down the app.
    """
    status_map = {}
    for sid in stub_ids:
        # We only care about the 'flags' (errors), discard the data
        _, flags = run_full_audit(sid)
        # 🔴 = Error, ✅ = Clean, ⚠️ = Warning (if you implemented warnings)
        status_map[sid] = "🔴" if flags else "✅"
    return status_map
    
# --- Database Helper ---
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

# --- TIMESHEET & CALCULATOR HELPERS ---

def get_pay_period_dates(period_ending_str):
    """Generates the 14 dates for a given period ending string."""
    end_date = datetime.strptime(period_ending_str, "%Y-%m-%d")
    dates = []
    # 13 days ago to today (total 14 days)
    start_date = end_date - timedelta(days=13)
    for i in range(14):
        d = start_date + timedelta(days=i)
        dates.append(d.strftime("%Y-%m-%d"))
    return dates

def load_timesheet(period_ending):
    """Loads existing timesheet data or creates a blank DataFrame."""
    conn = get_db()
    # Ensure table exists (handling migration simply here)
    conn.execute('''CREATE TABLE IF NOT EXISTS timesheet_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        period_ending TEXT,
        day_date TEXT,
        day_index INTEGER, 
        reg_hours REAL DEFAULT 0,
        ot_hours REAL DEFAULT 0,
        night_hours REAL DEFAULT 0,
        sunday_hours REAL DEFAULT 0,
        holiday_hours REAL DEFAULT 0,
        note TEXT,
        UNIQUE(period_ending, day_date)
    )''')
    
    existing = pd.read_sql("SELECT * FROM timesheet_entries WHERE period_ending = ? ORDER BY day_index ASC", 
                           conn, params=(period_ending,))
    conn.close()

    dates = get_pay_period_dates(period_ending)
    
    data = []
    for i, d in enumerate(dates):
        row = existing[existing['day_date'] == d] if not existing.empty else pd.DataFrame()
        if not row.empty:
            data.append({
                "Date": d,
                "Regular": row.iloc[0]['reg_hours'],
                "Overtime": row.iloc[0]['ot_hours'],
                "Night": row.iloc[0]['night_hours'],
                "Sunday": row.iloc[0]['sunday_hours'],
                "Holiday": row.iloc[0]['holiday_hours'],
                "Note": row.iloc[0]['note']
            })
        else:
            # Default: Workdays get 8, weekends 0
            weekday = datetime.strptime(d, "%Y-%m-%d").weekday() 
            is_weekend = weekday >= 5
            data.append({
                "Date": d,
                "Regular": 0.0 if is_weekend else 8.0,
                "Overtime": 0.0,
                "Night": 0.0,
                "Sunday": 0.0,
                "Holiday": 0.0,
                "Note": ""
            })
            
    return pd.DataFrame(data)

def save_timesheet(period_ending, df):
    conn = get_db()
    c = conn.cursor()
    for i, row in df.iterrows():
        c.execute("""
            INSERT INTO timesheet_entries (period_ending, day_date, day_index, reg_hours, ot_hours, night_hours, sunday_hours, holiday_hours, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(period_ending, day_date) DO UPDATE SET
            reg_hours=excluded.reg_hours,
            ot_hours=excluded.ot_hours,
            night_hours=excluded.night_hours,
            sunday_hours=excluded.sunday_hours,
            holiday_hours=excluded.holiday_hours,
            note=excluded.note
        """, (period_ending, row['Date'], i, row['Regular'], row['Overtime'], row['Night'], row['Sunday'], row['Holiday'], row['Note']))
    conn.commit()
    conn.close()

def calculate_expected_pay(timesheet_df, base_rate, actual_stub_meta):
    """
    Takes the timesheet and the base rate, returns a dict structure 
    identical to what 'run_full_audit' returns, but calculated from scratch.
    """
    total_reg = timesheet_df['Regular'].sum()
    total_ot = timesheet_df['Overtime'].sum()
    total_night = timesheet_df['Night'].sum()
    total_sun = timesheet_df['Sunday'].sum()
    total_hol = timesheet_df['Holiday'].sum()

    # --- 1. REGULAR ---
    amt_reg = round(total_reg * base_rate, 2)

    # --- 2. OVERTIME SPLIT (True OT + FLSA) ---
    if total_ot > 0:
        # A. True Overtime (The Straight Time Portion: 1.0x)
        amt_true_ot = round(total_ot * base_rate, 2)
        
        # B. FLSA Premium (The Half Time Portion: 0.5x)
        # CRITICAL: Agency rounds the half-rate to the penny BEFORE multiplying
        flsa_rate_calc = round(base_rate * 0.5, 2)
        amt_flsa = round(total_ot * flsa_rate_calc, 2)
    else:
        amt_true_ot = 0.0
        amt_flsa = 0.0

    # --- 3. DIFFERENTIALS ---
    amt_night = round(total_night * (base_rate * 0.10), 2)
    amt_sun = round(total_sun * (base_rate * 0.25), 2)
    amt_hol = round(total_hol * base_rate, 2)

    gross_pay = amt_reg + amt_true_ot + amt_flsa + amt_night + amt_sun + amt_hol

    # --- 4. BUILD ROWS ---
    earnings_rows = []
    
    if total_reg > 0: 
        earnings_rows.append(["Regular", base_rate, total_reg, amt_reg])
        
    if total_ot > 0:
        # Row 1: FLSA Premium (Rate displayed as 0.0 to match paystub)
        earnings_rows.append(["FLSA Premium", 0.0, total_ot, amt_flsa])
        # Row 2: True Overtime
        earnings_rows.append(["True Overtime", base_rate, total_ot, amt_true_ot])
        
    if total_night > 0: 
        earnings_rows.append(["Night Differential", base_rate * 0.10, total_night, amt_night])
    if total_sun > 0: 
        earnings_rows.append(["Sunday Premium", base_rate * 0.25, total_sun, amt_sun])
    if total_hol > 0: 
        earnings_rows.append(["Holiday Worked", base_rate, total_hol, amt_hol])

    earnings_df = pd.DataFrame(earnings_rows, columns=['type', 'rate', 'hours_current', 'amount_current'])
    earnings_df['amount_ytd'] = 0.0 
    earnings_df['hours_adjusted'] = 0.0 
    earnings_df['amount_adjusted'] = 0.0 

    stub = {
        'agency': actual_stub_meta['agency'],
        'period_ending': actual_stub_meta['period_ending'],
        'pay_date': actual_stub_meta['pay_date'],
        'gross_pay': gross_pay,
        'total_deductions': 0.0, 
        'net_pay': gross_pay, 
        'remarks': "GENERATED FROM USER TIMESHEET\n(Net Pay shown is Gross - 0 deductions)",
        'file_source': 'GENERATED'
    }

    return {'stub': stub, 'earnings': earnings_df, 'deductions': pd.DataFrame(), 'leave': pd.DataFrame()}

# --- Helper: Get Latest Baseline (Restored) ---
def get_latest_baseline():
    """Fetches the most recent 'real' paystub to use as a calculator model."""
    conn = get_db()
    # Get latest stub (excluding Shadow entries)
    query = """
        SELECT id, pay_date, period_ending, gross_pay, total_deductions
        FROM paystubs
        WHERE file_source IS NOT NULL AND file_source != 'SHADOW'
        ORDER BY pay_date DESC LIMIT 1
    """
    try:
        stub = pd.read_sql(query, conn).iloc[0]
    except (IndexError, pd.errors.DatabaseError):
        conn.close()
        raise Exception("No valid paystubs found in DB")

    # 1. FIXED DEDUCTIONS (Exclude Taxes)
    fixed_sql = """
        SELECT type, amount_current
        FROM deductions
        WHERE paystub_id = ?
        AND type NOT LIKE '%Tax%'
        AND type NOT LIKE '%OASDI%'
        AND type NOT LIKE '%Medicare%'
    """
    fixed_deductions = pd.read_sql(fixed_sql, conn, params=(int(stub['id']),))

    # Calculate Tax Rate
    total_actual_deductions = stub['total_deductions']
    sum_fixed = fixed_deductions['amount_current'].sum()
    variable_tax_amt = total_actual_deductions - sum_fixed
    tax_rate = variable_tax_amt / stub['gross_pay'] if stub['gross_pay'] > 0 else 0.0

    # Rates
    rates = pd.read_sql("SELECT type, rate FROM earnings WHERE paystub_id = ? AND rate > 0", conn, params=(int(stub['id']),))

    conn.close()
    return rates, tax_rate, fixed_deductions, stub['period_ending']

# --- Helper: Save Shadow Entry (Restored) ---
def save_shadow_entry(date, gross, net, details):
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO paystubs (pay_date, period_ending, gross_pay, net_pay, agency, file_source)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (date, date, gross, net, 'FAA-SHADOW', 'SHADOW'))
    conn.commit()
    conn.close()
    st.success(f"Shadow Entry for {date} saved!")

# --- Audit Logic ---
def run_full_audit(stub_id):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM paystubs WHERE id = ?", (stub_id,))
    stub = dict(cursor.fetchone())
    earnings = pd.read_sql("SELECT * FROM earnings WHERE paystub_id = ?", conn, params=(stub_id,))
    deductions = pd.read_sql("SELECT * FROM deductions WHERE paystub_id = ?", conn, params=(stub_id,))
    leave = pd.read_sql("SELECT * FROM leave_balances WHERE paystub_id = ?", conn, params=(stub_id,))
    conn.close()

    flags = {}

    # 1. Leave Math
    # These types don't carry a balance, so we skip the "Start + Earned - Used = End" check
    EXEMPT_LEAVE = ["Admin", "Change of Station Leave", "Time Off Award", "Gov Shutdown-Excepted"] 

    def to_minutes(val):
        """Converts 6.45 (6h 45m) into 405 minutes."""
        if val is None: return 0
        hours = int(val)
        minutes = round((val - hours) * 100) # .45 -> 45
        return (hours * 60) + minutes

    def to_dot_format(total_minutes):
        """Converts 405 minutes back to 6.45."""
        h = total_minutes // 60
        m = total_minutes % 60
        return h + (m / 100.0)

    for _, row in leave.iterrows():
        if row['type'] in EXEMPT_LEAVE:
            continue

        # Convert everything to raw minutes for safe math
        s_min = to_minutes(row['balance_start'])
        e_min = to_minutes(row['earned_current'])
        u_min = to_minutes(row['used_current'])
        end_actual_min = to_minutes(row['balance_end'])
        
        # Do the math
        calc_end_min = s_min + e_min - u_min
        
        # Check variance (1 minute tolerance)
        if abs(calc_end_min - end_actual_min) > 1:
            
            # Format nicely for the error message
            calc_h, calc_m = divmod(calc_end_min, 60)
            act_h, act_m = divmod(end_actual_min, 60)
            
            key = f"leave_{row['type']}_end"
            flags[key] = f"Math Error: {s_min//60}:{s_min%60:02d} + {e_min//60}:{e_min%60:02d} - {u_min//60}:{u_min%60:02d} should be {calc_h}:{calc_m:02d}, but stub says {act_h}:{act_m:02d}"

    # 2. Gross Math
    calc_gross = earnings['amount_current'].sum() + earnings['amount_adjusted'].sum()
    if abs(calc_gross - stub['gross_pay']) > 0.01:
        flags['gross_pay'] = f"Sum ({calc_gross:,.2f}) != Gross ({stub['gross_pay']:,.2f})"

    # 3. Net Math
    if abs((stub['gross_pay'] - stub['total_deductions']) - stub['net_pay']) > 0.01:
        flags['net_pay'] = f"Math Error: Gross - Ded != Net"

    return {'stub': stub, 'earnings': earnings, 'deductions': deductions, 'leave': leave}, flags

# --- THE VISUAL REPLICA ENGINE ---
def render_paystub(data, flags):
    stub = data['stub']

    def val(v, flag_id=None, is_money=True):
        txt = f"{v:,.2f}" if is_money and isinstance(v, (int, float)) else str(v)
        if flag_id and flag_id in flags:
            return f'<span class="audit-error" title="{flags[flag_id]}">{txt}</span>'
        return txt

    parts = []

    # 1. Container Start
    parts.append('<div class="stub-wrapper"><div id="elsInfoTable">')
    parts.append('<table class="table els-table" cellpadding="0" cellspacing="0"><tbody>')

    # 2. Header Section (Indentation Removed for HTML)
    parts.append(f'''
<tr>
    <td colspan="6" rowspan="2" class="col-6">
        <span id="lblAgencyName" class="text-align-center cell-title-lg2">{stub['agency']}</span><br>
        <span class="text-align-center cell-title-lg2">Earnings and Leave Statement</span>
    </td>
    <td colspan="3" class="col-3">
        <span class="cell-title">For Pay Period Ending</span>
        <span>{stub['period_ending']}</span>
    </td>
    <td colspan="3" class="col-3 no-margin-padding">
        <span class="cell-title blue">Net Pay</span>
        <span class="cell">$ {val(stub['net_pay'], 'net_pay')}</span>
    </td>
</tr>
<tr>
    <td colspan="3">
        <span class="cell-title">Pay Date</span>
        <span>{stub['pay_date']}</span>
    </td>
    <td colspan="3"></td>
</tr>
    ''')

    # 3. Summary Table
    parts.append(f'''
<tr>
    <td colspan="5" class="no-margin-padding">
        <table class="table no-margin-padding no-border">
            <tr>
                <th class="col-6 blue no-border">Your Pay Consists of</th>
                <th class="col-3 blue no-border text-align-right">Current</th>
            </tr>
            <tr>
                <td>Gross Pay</td>
                <td class="text-align-right">{val(stub['gross_pay'], 'gross_pay')}</td>
            </tr>
            <tr>
                <td>Total Deductions</td>
                <td class="text-align-right">{val(stub['total_deductions'])}</td>
            </tr>
            <tr>
                <td>Net Pay</td>
                <td class="text-align-right">{val(stub['net_pay'])}</td>
            </tr>
        </table>
    </td>
    <td colspan="7"></td>
</tr>
    ''')

    # 4. Earnings Header
    parts.append('''
<tr><td colspan="12" class="blue"><span class="text-align-center cell-title-lg">Earnings</span></td></tr>
<tr><td colspan="12"><table class="table no-border no-margin-padding">
<tr>
    <th class="col-5">Type</th>
    <th class="col-1 text-align-right">Rate</th>
    <th class="col-1 text-align-right">Hours</th>
    <th class="col-1 text-align-right">Current</th>
    <th class="col-1 text-align-right">YTD</th>
</tr>
    ''')

    # 5. Earnings Rows
    for _, row in data['earnings'].iterrows():
        parts.append(f'''
<tr>
    <td>{row['type']}</td>
    <td class="text-align-right">{val(row['rate'], is_money=False)}</td>
    <td class="text-align-right">{val(row['hours_current'], is_money=False)}</td>
    <td class="text-align-right">{val(row['amount_current'])}</td>
    <td class="text-align-right">{val(row['amount_ytd'])}</td>
</tr>
        ''')
    parts.append('</table></td></tr>')

    # 6. Deductions Header
    parts.append('''
<tr><td colspan="12" class="blue"><span class="text-align-center cell-title-lg">Deductions</span></td></tr>
<tr><td colspan="12"><table class="table no-border no-margin-padding">
<tr>
    <th class="col-4">Type</th>
    <th class="col-2 text-align-right">Current</th>
    <th class="col-2 text-align-right">YTD</th>
</tr>
    ''')

    # 7. Deductions Rows
    for _, row in data['deductions'].iterrows():
        ded_flag = f"deduction_{row['type']}"
        parts.append(f'''
<tr>
    <td>{val(row['type'], ded_flag, is_money=False)}</td>
    <td class="text-align-right">{val(row['amount_current'])}</td>
    <td class="text-align-right">{val(row['amount_ytd'])}</td>
</tr>
        ''')
    parts.append('</table></td></tr>')

    # 8. Leave Header
    parts.append('''
<tr><td colspan="12" class="blue"><span class="text-align-center cell-title-lg">Leave</span></td></tr>
<tr><td colspan="12"><table class="table no-border no-margin-padding">
<tr>
    <th class="col-2">Type</th>
    <th class="col-1 text-align-right">Start Bal</th>
    <th class="col-1 text-align-right">Earned</th>
    <th class="col-1 text-align-right">Used</th>
    <th class="col-1 text-align-right">End Bal</th>
</tr>
    ''')

    # 9. Leave Rows
    for _, row in data['leave'].iterrows():
        end_flag = f"leave_{row['type']}_end"
        parts.append(f'''
<tr>
    <td>{row['type']}</td>
    <td class="text-align-right">{val(row['balance_start'], is_money=False)}</td>
    <td class="text-align-right">{val(row['earned_current'], is_money=False)}</td>
    <td class="text-align-right">{val(row['used_current'], is_money=False)}</td>
    <td class="text-align-right">{val(row['balance_end'], end_flag, is_money=False)}</td>
</tr>
        ''')
    parts.append('</table></td></tr>')

    # 10. Remarks Section
    remarks_text = stub.get('remarks', '').replace('\n', '<br>') if stub.get('remarks') else ""
    
    parts.append(f'''
    <tr><td colspan="12" class="blue"><span class="text-align-center cell-title-lg">Remarks</span></td></tr>
    <tr>
        <td colspan="12" style="padding: 10px;">
            <span id="lblRemarks" style="font-family: monospace; white-space: pre-wrap;">{remarks_text}</span>
        </td>
    </tr>
    ''')
    
    parts.append('</tbody></table></div></div>')
    return "".join(parts)

# --- TABS ---
tab_proj, tab_audit, tab_graphs, tab_ingest = st.tabs(["🔮 Projection", "🧐 Audit & View", "📊 Graphs", "📥 Ingestion"])

# --- TAB: PROJECTION ---
with tab_proj:
    st.header("Project Next Paycheck")
    try:
        rates, tax_rate, fixed_deductions, last_period_end = get_latest_baseline()
        st.info(f"Baseline: Using rates from Pay Period Ending **{last_period_end}** | Eff. Tax Rate: **{tax_rate*100:.1f}%**")

        col1, col2 = st.columns([1, 2])
        with col1:
            st.subheader("Hours Input")
            # Dynamic inputs with safety check
            reg_rate_row = rates[rates['type'].str.contains('Regular', case=False)]
            reg_rate = reg_rate_row['rate'].values[0] if not reg_rate_row.empty else 0.0

            reg_hrs = st.number_input("Regular Hours", value=80.0, step=8.0)
            ot_hrs = st.number_input("Overtime Hours", value=0.0, step=1.0)
            night_hrs = st.number_input("Night Diff Hours", value=0.0, step=1.0)
            sun_hrs = st.number_input("Sunday Hours", value=0.0, step=1.0)
            shadow_date = st.date_input("Target Pay Date")

        with col2:
            st.subheader("Calculation")
            gross_pay = (reg_hrs * reg_rate)
            if ot_hrs > 0: gross_pay += (ot_hrs * (reg_rate * 1.5))
            if night_hrs > 0: gross_pay += (night_hrs * (reg_rate * 0.10))
            if sun_hrs > 0: gross_pay += (sun_hrs * (reg_rate * 0.25))

            est_tax = gross_pay * tax_rate
            total_fixed = fixed_deductions['amount_current'].sum()
            net_pay = gross_pay - est_tax - total_fixed

            m1, m2, m3 = st.columns(3)
            m1.metric("Gross Pay", f"${gross_pay:,.2f}")
            m2.metric("Est. Net Pay", f"${net_pay:,.2f}")
            m3.metric("Tax Hit", f"${est_tax:,.2f}")

            with st.expander("See Fixed Deductions Breakdown"):
                st.dataframe(fixed_deductions)

            if st.button("💾 Save to Shadow Ledger"):
                save_shadow_entry(shadow_date, gross_pay, net_pay, "Details")

    except Exception as e:
        st.error(f"Error loading baseline: {e}. Try ingesting data first.")

# --- TAB: AUDIT & VIEW ---
with tab_audit:
    # Inject Grid CSS specifically for this tab
    st.markdown("""
    <style>
        .comparison-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .comp-col h3 { text-align: center; padding: 10px; color: white; border-radius: 5px; }
        .comp-expected { border-top: 5px solid #2e86c1; }
        .comp-actual { border-top: 5px solid #27ae60; }
    </style>
    """, unsafe_allow_html=True)

    st.header("Deep Dive Audit")
    
    conn = get_db()
    # Added period_ending to this query
    stubs = pd.read_sql("SELECT id, pay_date, period_ending, net_pay, file_source FROM paystubs ORDER BY pay_date DESC", conn)
    conn.close()
    
    if not stubs.empty:
        # 1. Pre-calculate status (Keep your existing function call)
        status_map = get_audit_status_map(stubs['id'].tolist())

        # 2. Dropdown
        def fmt(row_id):
            r = stubs[stubs['id'] == row_id].iloc[0]
            icon = status_map.get(row_id, "❓")
            return f"{icon} {r['period_ending']} (Net: ${r['net_pay']:,.2f})"

        selected_id = st.selectbox("Select Pay Period:", stubs['id'].tolist(), format_func=fmt)
        
        # 3. Load Actual Data
        actual_data, flags = run_full_audit(selected_id)
        current_period_ending = actual_data['stub']['period_ending']

        # 4. TIMESHEET INPUT SECTION
        with st.expander("📝 Variance Analysis (Edit Timesheet)", expanded=True):
            ts_df = load_timesheet(current_period_ending)
            
            edited_df = st.data_editor(
                ts_df, 
                num_rows="fixed", 
                hide_index=True,
                column_config={
                    "Date": st.column_config.TextColumn(disabled=True),
                    "Regular": st.column_config.NumberColumn(format="%.1f"),
                    "Overtime": st.column_config.NumberColumn(format="%.1f"),
                    "Night": st.column_config.NumberColumn(format="%.1f"),
                    "Sunday": st.column_config.NumberColumn(format="%.1f"),
                    "Holiday": st.column_config.NumberColumn(format="%.1f"),
                }
            )
            
            if st.button("💾 Save Inputs & Recalculate"):
                save_timesheet(current_period_ending, edited_df)
                st.rerun()

        # 5. Calculate Expected Data
        base_rate = 0.0
        
        # A. Try to get rate from CURRENT stub
        reg_rows = actual_data['earnings'][actual_data['earnings']['type'].str.contains('Regular', case=False, na=False)]
        if not reg_rows.empty:
            base_rate = reg_rows.iloc[0]['rate']
            
        # B. FAILSAFE: If rate is missing/zero (e.g., Shutdown or $0.00 check), find last known good rate
        if base_rate == 0.0:
            conn = get_db()
            # Find the most recent 'Regular' entry with a positive rate
            last_known = pd.read_sql("""
                SELECT rate FROM earnings 
                WHERE type LIKE '%Regular%' AND rate > 0 
                ORDER BY id DESC LIMIT 1
            """, conn)
            conn.close()
            
            if not last_known.empty:
                base_rate = last_known.iloc[0]['rate']
                st.info(f"ℹ️ Current stub has no rate (Shutdown?). Using last known rate: ${base_rate:.2f}/hr")
            else:
                st.error("⚠️ Could not find ANY historical pay rate in the database. Please ingest at least one valid paycheck.")

        expected_data = calculate_expected_pay(edited_df, base_rate, actual_data['stub'])

        # 6. RENDER SIDE-BY-SIDE
        if flags:
            st.error(f"⚠️ Found {len(flags)} Anomalies in the Actual Paystub! See Red text below.")
        else:
            st.success("✅ Math Checks Passed on Actual Stub.")

        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown('<div class="comp-col comp-expected"><h3 style="background-color: #2e86c1;">🟦 Your Calculation</h3></div>', unsafe_allow_html=True)
            # We reuse your existing render_paystub function
            html_expected = render_paystub(expected_data, {}) 
            st.markdown(html_expected, unsafe_allow_html=True)

        with col2:
            st.markdown('<div class="comp-col comp-actual"><h3 style="background-color: #27ae60;">🟩 Official Paystub</h3></div>', unsafe_allow_html=True)
            # We reuse your existing render_paystub function
            html_actual = render_paystub(actual_data, flags)
            st.markdown(html_actual, unsafe_allow_html=True)

    else:
        st.warning("No paystubs found. Go to 'Ingestion' tab.")

# --- TAB: GRAPHS ---
with tab_graphs:
    st.header("Financial Trends")
    conn = get_db()
    df = pd.read_sql("SELECT pay_date, gross_pay, net_pay FROM paystubs ORDER BY pay_date ASC", conn)
    conn.close()
    if not df.empty:
        st.line_chart(df.set_index('pay_date')[['gross_pay', 'net_pay']])
    else:
        st.info("No data.")

# --- TAB: INGESTION ---
with tab_ingest:
    if st.button("Scan PayStubs Folder"):
        os.system("python3 ingest.py")
        st.success("Scan processed.")
