import streamlit as st
import pandas as pd
import models
import logic
import views
import os
from datetime import datetime, timedelta

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
    st.header("Deep Dive Audit")
    
    # 1. Fetch Stubs & Calculate Next Period
    stubs = models.get_paystubs_meta()
    
    # --- LOGIC: Predict Current/Next Period ---
    next_pe = None
    if not stubs.empty:
        stubs = stubs.sort_values('period_ending', ascending=False)
        last_pe_str = stubs.iloc[0]['period_ending']
        last_dt = datetime.strptime(last_pe_str, "%Y-%m-%d")
        next_dt = last_dt + timedelta(days=14)
        next_pe = next_dt.strftime("%Y-%m-%d")
        
        # Create a "Projected" row
        future_row = pd.DataFrame([{
            'id': -1, 
            'pay_date': 'Pending', 
            'period_ending': next_pe, 
            'net_pay': 0.0, 
            'gross_pay': 0.0, 
            'file_source': 'Projected'
        }])
        stubs = pd.concat([future_row, stubs], ignore_index=True)

    if not stubs.empty:
        # Pre-calculate statuses
        status_map = {}
        status_map[-1] = "📅" 
        
        for _, row in stubs.iterrows():
            sid = row['id']
            if sid == -1: continue 
            d = models.get_full_paystub_data(sid)
            f = logic.run_full_audit(d)
            status_map[sid] = "🔴" if f else "✅"

        def fmt(rid): 
            if rid == -1:
                return f"{status_map.get(rid)} Current (Projected): {next_pe}"
            r = stubs[stubs['id']==rid].iloc[0]
            icon = status_map.get(rid, "")
            return f"{icon} {r['period_ending']} (Net: ${r['net_pay']:,.2f})"

        sel_id = st.selectbox("Select Pay Period:", stubs['id'], format_func=fmt)
        
        # 2. Setup Context (Projected vs Actual)
        if sel_id == -1:
            # PROJECTED MODE
            pe = next_pe
            act_data = None
            act_flags = {}
            st.info(f"You are editing the current pay period ({pe}). We are using your last known pay rates for estimates.")
        else:
            # ACTUAL MODE
            act_data = models.get_full_paystub_data(sel_id)
            act_flags = logic.run_full_audit(act_data)
            pe = act_data['stub']['period_ending']

        # 3. V2 Editor
        with st.expander("📝 Edit Schedule (Actual Worked)", expanded=True):
            ts_v2 = models.load_timesheet_v2(pe)
            
            # Use same regex here to allow clearing shifts
            time_regex = r"^$|^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$"

            edited = st.data_editor(
                ts_v2, 
                num_rows="fixed", 
                hide_index=True,
                width="stretch",
                column_config={
                    "Date": st.column_config.DateColumn(format="MM-DD (ddd)", disabled=True),
                    "Start": st.column_config.TextColumn("Act Start", validate=time_regex),
                    "End": st.column_config.TextColumn("Act End", validate=time_regex),
                    "Leave_Type": st.column_config.SelectboxColumn("Leave Type (if gap)", options=["Annual", "Sick", "Credit", "Comp", "LWOP"]),
                    "OJTI": st.column_config.NumberColumn("OJTI (Hrs)"),
                    "CIC": st.column_config.NumberColumn("CIC (Hrs)")
                }
            )
            
            if st.button("💾 Calculate"):
                models.save_timesheet_v2(pe, edited)
                
                conn = models.get_db()
                std_sched = pd.read_sql("SELECT * FROM user_schedule", conn).set_index('day_of_week')
                conn.close()
                
                bucket_rows = []
                for _, row in edited.iterrows():
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
