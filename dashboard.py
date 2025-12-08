import streamlit as st
import sqlite3
import pandas as pd
import os

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
    st.header("Deep Dive Audit")
    
    conn = get_db()
    # UPDATED: Added 'period_ending' to the SELECT statement
    stubs = pd.read_sql("SELECT id, pay_date, period_ending, net_pay, file_source FROM paystubs ORDER BY pay_date DESC", conn)
    conn.close()
    
    if not stubs.empty:
        # 1. Pre-calculate status for all stubs (Cached)
        status_map = get_audit_status_map(stubs['id'].tolist())

        # 2. Custom Formatting for Dropdown
        def fmt(row_id):
            r = stubs[stubs['id'] == row_id].iloc[0]
            
            # Icon based on audit result
            icon = status_map.get(row_id, "❓")
            
            # Shadow tag
            tag = " [SHADOW]" if r['file_source'] == 'SHADOW' else ""
            
            # UPDATED: Changed label to "For pay period ending [Date]"
            return f"{icon}{tag} For pay period ending {r['period_ending']} (Net: ${r['net_pay']:,.2f})"

        # 3. The Menu
        selected_id = st.selectbox(
            "Select Pay Period (🔴=Error, ✅=Clean):", 
            options=stubs['id'].tolist(), 
            format_func=fmt
        )
        
        # 4. Run Full Logic for Display
        data, flags = run_full_audit(selected_id)
        
        if flags:
            st.error(f"⚠️ Found {len(flags)} Anomalies! Scroll down to see red items.")
        else:
            st.success("✅ All Math Checks Passed.")

        html_view = render_paystub(data, flags)
        st.markdown(html_view, unsafe_allow_html=True)
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
