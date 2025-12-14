import streamlit as st
import pandas as pd
import models
import logic
import views
import os
from datetime import datetime, timedelta
import socket
import subprocess
import sys
import time

def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0

# Check if Listener (Port 5000) is running. If not, launch it.
if not is_port_in_use(5000):
    # Determine the path to listener.py relative to this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    listener_path = os.path.join(script_dir, "listener.py")
    
    # Spawn the process in the background
    subprocess.Popen([sys.executable, listener_path])
    
    # Optional: Brief wait to let it spin up
    time.sleep(1)
# --------------------------------

st.set_page_config(page_title="FAA PayTracker", layout="wide")
st.markdown(views.get_css(), unsafe_allow_html=True)
models.setup_database()

tab_audit, tab_graphs, tab_facts, tab_ingest = st.tabs(["🧐 Audit & Time", "📊 Graphs", "ℹ️ Basic Facts", "📥 Ingestion"])

# --- TAB: BASIC FACTS (Schedule Setup) ---
with tab_facts:
    st.header("My Standard Schedule")
    st.info("Enter times as HH:MM (e.g. 07:00 or 15:30). Leave empty for RDOs.")
    
    conn = models.get_db()
    sched_df = pd.read_sql("SELECT * FROM user_schedule ORDER BY day_of_week", conn)
    conn.close()
    
    days_map = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}
    sched_df['Day'] = sched_df['day_of_week'].map(days_map)
    sched_df = sched_df[['Day', 'start_time', 'end_time', 'day_of_week']]
    
    # --- REGEX FIX: Allow Empty String (^$) OR Time Format ---
    time_regex = r"^$|^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$"

    edited_sched = st.data_editor(
        sched_df,
        hide_index=True,
        width="stretch",
        column_config={
            "day_of_week": None, 
            "Day": st.column_config.TextColumn(disabled=True),
            "start_time": st.column_config.TextColumn("Std Start", validate=time_regex),
            "end_time": st.column_config.TextColumn("Std End", validate=time_regex)
        },
        disabled=["Day"]
    )
    
    if st.button("💾 Save Standard Schedule"):
        models.save_user_schedule(edited_sched)
        st.success("Standard schedule updated!")
        st.rerun()

# --- TAB: AUDIT ---
with tab_audit:
    # ==========================
    # Shutdown Reconciliation
    # ==========================
    st.header("🏛️ Shutdown Reconciliation")
    
    with st.expander("View Cumulative Ledger", expanded=False):
        # 1. Fetch Data
        stubs_for_ledger = models.get_paystubs_meta()
        
        # 2. Reference Data (Grab most recent for rate estimation)
        ref_rate, ref_ded, ref_earn = models.get_reference_data(
            stubs_for_ledger.iloc[0]['id'] if not stubs_for_ledger.empty else 1
        )
        
        # 3. Schedule for Fallbacks
        conn = models.get_db()
        std_sched = pd.read_sql("SELECT * FROM user_schedule", conn).set_index('day_of_week')
        conn.close()

        if not stubs_for_ledger.empty:
            # 4. Run Logic
            ledger_df = logic.generate_shutdown_ledger(stubs_for_ledger, ref_rate, ref_ded, ref_earn, std_sched)

            # 5. Calculate Metrics
            # We filter for rows that are NOT 'Unaudited' to get the real debt
            audited_rows = ledger_df[ledger_df['Status'] != "⚪ Unaudited"]
            current_balance = audited_rows.iloc[-1]['Balance'] if not audited_rows.empty else 0.0
            
            # 6. Display Top-Level Metrics
            m1, m2 = st.columns([1, 3])
            with m1:
                if current_balance < -1.0:
                    st.error(f"⚠️ Gov Owes You\n# ${abs(current_balance):,.2f}")
                elif current_balance > 1.0:
                    st.success(f"💰 Overpaid/Bonus\n# ${current_balance:,.2f}")
                else:
                    st.info(f"✅ Settled\n# $0.00")
            
            with m2:
                # Simple style map for the dataframe
                def highlight_status(val):
                    if "Gov Owes" in val: return 'background-color: #ffcccc; color: black;'
                    if "Backpay" in val: return 'background-color: #ccffcc; color: black;'
                    return ''

                st.dataframe(
                    ledger_df.style.format({
                        "Expected": "${:,.2f}", 
                        "Actual": "${:,.2f}", 
                        "Diff": "${:,.2f}", 
                        "Balance": "${:,.2f}"
                    }).map(highlight_status, subset=['Status']),
                    width="stretch",
                    hide_index=True
                )
        else:
            st.warning("No paystubs found to generate ledger.")
            
    st.divider()

    # ==========================
    # Begin Deep Dive Audit code
    # ==========================
    st.header("Deep Dive Audit")
    
    # 1. Fetch Stubs & Calculate Next Periods
    stubs = models.get_paystubs_meta()
    
    # --- LOGIC: Predict Future Periods (Projected #1 and #2) ---
    # We need 2 future periods to cover the "Gap Week" where the current pay period 
    # has ended but the official stub hasn't been released yet.
    if not stubs.empty:
        stubs = stubs.sort_values('period_ending', ascending=False)
        last_pe_str = stubs.iloc[0]['period_ending']
        last_dt = datetime.strptime(last_pe_str, "%Y-%m-%d")
        
        future_rows = []
        for i in range(1, 3): # Generate +14 days (ID -1) and +28 days (ID -2)
            fut_dt = last_dt + timedelta(days=14 * i)
            fut_pe = fut_dt.strftime("%Y-%m-%d")
            future_rows.append({
                'id': -1 * i, 
                'pay_date': 'Pending', 
                'period_ending': fut_pe, 
                'net_pay': 0.0, 
                'gross_pay': 0.0, 
                'file_source': 'Projected'
            })
            
        future_df = pd.DataFrame(future_rows)
        stubs = pd.concat([future_df, stubs], ignore_index=True)

    if not stubs.empty:
        # Pre-calculate statuses
        status_map = {}
        status_map[-1] = "📅" 
        status_map[-2] = "🔮"
        
        for _, row in stubs.iterrows():
            sid = row['id']
            if sid < 0: continue  # Skip audit for projected
            d = models.get_full_paystub_data(sid)
            f = logic.run_full_audit(d)
            status_map[sid] = "🔴" if f else "✅"

        def fmt(rid): 
            # Look up the date for this specific ID in the dataframe
            row_meta = stubs[stubs['id']==rid].iloc[0]
            pe_str = row_meta['period_ending']
            
            if rid < 0:
                label = "Current" if rid == -1 else "Next"
                return f"{status_map.get(rid)} {label} (Projected): {pe_str}"
            
            r = stubs[stubs['id']==rid].iloc[0]
            icon = status_map.get(rid, "")
            return f"{icon} {r['period_ending']} (Net: ${r['net_pay']:,.2f})"

        sel_id = st.selectbox("Select Pay Period:", stubs['id'], format_func=fmt)

        # If the user switches the dropdown, clear the previous calculation
        if sel_id != st.session_state.get('last_viewed_id'):
            st.session_state['res'] = None
            st.session_state['last_viewed_id'] = sel_id
        
        # 2. Setup Context (Projected vs Actual)
        # We now dynamically grab 'pe' from the dataframe so it works for -1 OR -2
        selected_row = stubs[stubs['id'] == sel_id].iloc[0]
        pe = selected_row['period_ending']

        if sel_id < 0:
            # PROJECTED MODE (Handles both -1 and -2)
            act_data = None
            act_flags = {}
            st.info(f"You are editing a projected period ({pe}). We are using your last known pay rates for estimates.")
        else:
            # ACTUAL MODE
            act_data = models.get_full_paystub_data(sel_id)
            act_flags = logic.run_full_audit(act_data)
            # Ensure pe is consistent (though it should be same as lookup)
            pe = act_data['stub']['period_ending']

        # 3. V2 Editor
        with st.expander("📝 Edit Schedule (Actual Worked)", expanded=True):
            ts_v2 = models.load_timesheet_v2(pe)

            # --- HELPER FUNCTIONS FOR TIME CONVERSION ---
            def float_to_hhmm(val):
                """Converts 1.266 -> '1:16'"""
                try:
                    val = float(val)
                    if val <= 0: return ""
                    h = int(val)
                    m = int(round((val - h) * 60))
                    return f"{h}:{m:02d}"
                except (ValueError, TypeError):
                    return ""

            def hhmm_to_float(val):
                """Converts '1:16' -> 1.266"""
                if not val or val == "": return 0.0
                if isinstance(val, (int, float)): return float(val)
                if ":" in str(val):
                    parts = str(val).split(":")
                    # Handle 1:16
                    return float(parts[0]) + (float(parts[1]) / 60.0)
                return 0.0
            # ---------------------------------------------

            # 1. PREPARE DATA FOR DISPLAY (Convert Decimals to HH:MM strings)
            # We create display columns so the user sees "1:16" instead of 1.266666
            ts_v2['OJTI'] = ts_v2['OJTI'].apply(float_to_hhmm)
            ts_v2['CIC'] = ts_v2['CIC'].apply(float_to_hhmm)

            # Logic: If we haven't calculated yet (res is None) BUT we have saved data in DB,
            # auto-run the calculation so the user sees their saved state immediately.
            if st.session_state.get('res') is None and models.has_saved_timesheet(pe):
                
                # A. Convert strings back to floats for the logic engine (Temporary for this Auto-Run)
                temp_df = ts_v2.copy()
                temp_df['OJTI'] = temp_df['OJTI'].apply(hhmm_to_float)
                temp_df['CIC'] = temp_df['CIC'].apply(hhmm_to_float)

                # B. Fetch Dependencies
                conn = models.get_db()
                std_sched = pd.read_sql("SELECT * FROM user_schedule", conn).set_index('day_of_week')
                conn.close()
                ref_rate, ref_ded, ref_earn = models.get_reference_data(sel_id)

                # C. Re-run Bucket Logic
                bucket_rows = []
                for _, row in temp_df.iterrows():
                    s_obj = pd.to_datetime(row['Start'], format='%H:%M').time() if row['Start'] else None
                    e_obj = pd.to_datetime(row['End'], format='%H:%M').time() if row['End'] else None
                    
                    b = logic.calculate_daily_breakdown(
                        row['Date'], s_obj, e_obj, row['Leave_Type'], 
                        row['OJTI'], row['CIC'], std_sched
                    )
                    bucket_rows.append(b)
                buckets = pd.DataFrame(bucket_rows, columns=["Regular", "Overtime", "Night", "Sunday", "Holiday", "Hol_Leave", "OJTI", "CIC"])

                # D. Setup Metadata for Calculator
                if act_data:
                    stub_meta = act_data['stub']
                    stub_leave = act_data['leave']
                else:
                    stub_meta = {
                        'agency': 'Federal Aviation Administration',
                        'period_ending': pe,
                        'pay_date': 'Estimated',
                        'gross_pay': 0.0, 'net_pay': 0.0, 'total_deductions': 0.0,
                        'remarks': 'PROJECTED ESTIMATE'
                    }
                    stub_leave = pd.DataFrame()

                # E. Run & Save to Session State
                exp_data = logic.calculate_expected_pay(buckets, ref_rate, stub_meta, ref_ded, stub_leave, ref_earn)
                st.session_state['res'] = exp_data
                st.rerun()
            
            # Use same regex here to allow clearing shifts
            time_regex = r"^$|^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$"
            
            # --- FEATURE: Mark Observed Holidays Visually ---
            conn = models.get_db()
            std_sched = pd.read_sql("SELECT * FROM user_schedule", conn).set_index('day_of_week')
            conn.close()
            
            # Create a display column
            ts_v2['Display_Date'] = ts_v2['Date']
            
            for idx, row in ts_v2.iterrows():
                d_obj = datetime.strptime(row['Date'], "%Y-%m-%d").date()
                
                # 1. Restore the Day of Week Format (e.g. "10-04 (Sat)")
                day_str = d_obj.strftime("%m-%d (%a)")
                ts_v2.at[idx, 'Display_Date'] = day_str
                
                # 2. Check Holiday Logic
                obs_date = logic.get_observed_holiday(d_obj, std_sched)
                
                is_obs = False
                for h in logic.HOLIDAYS:
                    h_d = datetime.strptime(h, "%Y-%m-%d").date()
                    if logic.get_observed_holiday(h_d, std_sched) == d_obj:
                        is_obs = True
                        break
                
                if is_obs:
                    # Append the flag to the formatted string
                    ts_v2.at[idx, 'Display_Date'] = f"{day_str} (HOLIDAY) 🎉"
            # ------------------------------------------------

            edited = st.data_editor(
                ts_v2, 
                num_rows="fixed", 
                hide_index=True,
                width="stretch",
                column_config={
                    "Date": None, 
                    "Display_Date": st.column_config.TextColumn("Date", disabled=True),
                    "Start": st.column_config.TextColumn("Act Start", validate=time_regex),
                    "End": st.column_config.TextColumn("Act End", validate=time_regex),
                    "Leave_Type": st.column_config.SelectboxColumn(
                        "Leave Type (if gap)", 
                        options=["Holiday", "Annual", "Sick", "Credit", "Comp", "LWOP"]
                    ),
                    # CHANGED: TextColumn to allow HH:MM input
                    "OJTI": st.column_config.TextColumn("OJTI (HH:MM)", validate=time_regex),
                    "CIC": st.column_config.TextColumn("CIC (HH:MM)", validate=time_regex)
                },
                column_order=["Display_Date", "Start", "End", "Leave_Type", "OJTI", "CIC"]
            )
            
            # Restore raw date for saving
            edited['Date'] = ts_v2['Date']
            
            if st.button("💾 Calculate"):
                # 2. CONVERT BACK TO FLOATS FOR STORAGE/MATH
                # If we don't do this, logic.py will crash trying to do math on "1:30"
                calc_df = edited.copy()
                calc_df['OJTI'] = calc_df['OJTI'].apply(hhmm_to_float)
                calc_df['CIC'] = calc_df['CIC'].apply(hhmm_to_float)

                models.save_timesheet_v2(pe, calc_df)
                
                conn = models.get_db()
                std_sched = pd.read_sql("SELECT * FROM user_schedule", conn).set_index('day_of_week')
                conn.close()
                
                bucket_rows = []
                for _, row in calc_df.iterrows():
                    s_obj = pd.to_datetime(row['Start'], format='%H:%M').time() if row['Start'] else None
                    e_obj = pd.to_datetime(row['End'], format='%H:%M').time() if row['End'] else None
                    
                    b = logic.calculate_daily_breakdown(
                        row['Date'], s_obj, e_obj, row['Leave_Type'], 
                        row['OJTI'], row['CIC'], std_sched
                    )
                    bucket_rows.append(b)
                
                buckets = pd.DataFrame(bucket_rows, columns=["Regular", "Overtime", "Night", "Sunday", "Holiday", "Hol_Leave", "OJTI", "CIC"])
                
                # Pay Engine Setup
                ref_rate, ref_ded, ref_earn = models.get_reference_data(sel_id)
                
                if act_data:
                    stub_meta = act_data['stub']
                    stub_leave = act_data['leave']
                else:
                    # Robust dummy for projections
                    stub_meta = {
                        'agency': 'Federal Aviation Administration',
                        'period_ending': pe,
                        'pay_date': 'Estimated',
                        'gross_pay': 0.0,
                        'net_pay': 0.0,
                        'total_deductions': 0.0,
                        'remarks': 'PROJECTED ESTIMATE'
                    }
                    stub_leave = pd.DataFrame()

                exp_data = logic.calculate_expected_pay(buckets, ref_rate, stub_meta, ref_ded, stub_leave, ref_earn)
                st.session_state['res'] = exp_data

        # 4. Render
        exp_data = st.session_state.get('res', None)
        
        # If Projected and no calculation yet, run a default one
        if not exp_data and sel_id == -1:
             ref_rate, ref_ded, ref_earn = models.get_reference_data(sel_id)
             empty_buckets = pd.DataFrame(columns=["Regular", "Overtime", "Night", "Sunday", "Holiday", "Hol_Leave", "OJTI", "CIC"])
             
             dummy_stub = {
                'agency': 'Federal Aviation Administration',
                'period_ending': pe,
                'pay_date': 'Estimated',
                'gross_pay': 0.0, 'net_pay': 0.0, 'total_deductions': 0.0,
                'remarks': 'PROJECTED ESTIMATE'
             }
             
             exp_data = logic.calculate_expected_pay(empty_buckets, ref_rate, dummy_stub, ref_ded, pd.DataFrame(), ref_earn)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown('<div class="comp-col comp-expected"><h3 style="background-color: #2e86c1;">🟦 Your Calculation</h3></div>', unsafe_allow_html=True)
            if exp_data:
                st.markdown(views.render_paystub_html(exp_data, mode="expected"), unsafe_allow_html=True)
        with c2:
            st.markdown('<div class="comp-col comp-actual"><h3 style="background-color: #27ae60;">🟩 Official Paystub</h3></div>', unsafe_allow_html=True)
            if act_data:
                st.markdown(views.render_paystub_html(act_data, act_flags, mode="actual"), unsafe_allow_html=True)
            else:
                st.markdown("""
                <div style="text-align: center; padding: 40px; color: #666; background: #f9f9f9; border: 1px dashed #ccc;">
                    <h3>No Official Data Yet</h3>
                    <p>This paystub has not been imported.</p>
                    <p>Once imported, your manual entries will be matched automatically.</p>
                </div>
                """, unsafe_allow_html=True)
    else:
        st.warning("No data found. Please run an ingestion scan first to seed the database.")
    
with tab_graphs:
    st.header("Financial Trends")
    stubs = models.get_paystubs_meta()
    if not stubs.empty:
        st.line_chart(stubs.set_index('pay_date')[['gross_pay', 'net_pay']])

with tab_ingest:
    if st.button("Scan PayStubs"):
        os.system("python3 ingest.py")
        st.success("Scan processed.")
