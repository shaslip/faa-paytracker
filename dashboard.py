import streamlit as st
import pandas as pd
import models
import logic
import views
import os

st.set_page_config(page_title="FAA PayTracker", layout="wide")
st.markdown(views.get_css(), unsafe_allow_html=True)
models.setup_database()

tab_audit, tab_graphs, tab_facts, tab_ingest = st.tabs(["🧐 Audit & Time", "📊 Graphs", "ℹ️ Basic Facts", "📥 Ingestion"])

# --- TAB: BASIC FACTS (Schedule Setup) ---
with tab_facts:
    st.header("My Standard Schedule")
    st.info("Set your default start/end times. Leave them empty for days off.")
    
    # Load current schedule
    conn = models.get_db()
    sched_df = pd.read_sql("SELECT * FROM user_schedule ORDER BY day_of_week", conn)
    conn.close()
    
    # --- FIX 1: Strict Data Conversion for Streamlit Editor ---
    # Streamlit freezes if it sees Pandas 'NaT'. We must convert NaT -> None.
    s_temp = pd.to_datetime(sched_df['start_time'], format='%H:%M', errors='coerce')
    sched_df['start_time'] = s_temp.dt.time.where(s_temp.notna(), None)
    
    e_temp = pd.to_datetime(sched_df['end_time'], format='%H:%M', errors='coerce')
    sched_df['end_time'] = e_temp.dt.time.where(e_temp.notna(), None)
    # ---------------------------------------------------------

    # Map integers 0-6 to Monday-Sunday
    days_map = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}
    sched_df['Day'] = sched_df['day_of_week'].map(days_map)
    
    # Reorder - Removed 'is_workday' from view
    sched_df = sched_df[['Day', 'start_time', 'end_time', 'day_of_week']]
    
    edited_sched = st.data_editor(
        sched_df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "day_of_week": None, 
            "Day": st.column_config.TextColumn(disabled=True),
            "start_time": st.column_config.TimeColumn("Std Start", format="HH:mm", step=60),
            "end_time": st.column_config.TimeColumn("Std End", format="HH:mm", step=60)
        },
        disabled=["Day"]
    )
    
    if st.button("💾 Save Standard Schedule"):
        models.save_user_schedule(edited_sched)
        st.success("Standard schedule updated!")
        st.rerun()

with tab_audit:
    st.header("Deep Dive Audit")
    
    # 1. Select Stub (Optimized)
    stubs = models.get_paystubs_meta()
    
    if not stubs.empty:
        # --- OPTIMIZATION START ---
        # Pre-calculate statuses effectively to prevent N+1 query lag in the selectbox
        status_map = {}
        for _, row in stubs.iterrows():
            sid = row['id']
            # We fetch data once per row here.
            # If this is still too slow with 100+ stubs, move this logic to the SQL query or a 'flags' column in DB.
            d = models.get_full_paystub_data(sid)
            f = logic.run_full_audit(d)
            status_map[sid] = "🔴" if f else "✅"
        # --- OPTIMIZATION END ---

        def fmt(rid): 
            r = stubs[stubs['id']==rid].iloc[0]
            # Use the pre-calculated map
            icon = status_map.get(rid, "")
            return f"{icon} {r['period_ending']} (Net: ${r['net_pay']:,.2f})"

        sel_id = st.selectbox("Select Pay Period:", stubs['id'], format_func=fmt)
        
        # 2. Get Actuals & Reference
        act_data = models.get_full_paystub_data(sel_id)
        act_flags = logic.run_full_audit(act_data)
        pe = act_data['stub']['period_ending']

        # 3. V2 Editor (Start/End Times)
        with st.expander("📝 Edit Schedule (Actual Worked)", expanded=True):
            ts_v2 = models.load_timesheet_v2(pe)
            
            edited = st.data_editor(
                ts_v2, 
                num_rows="fixed", 
                hide_index=True,
                column_config={
                    "Date": st.column_config.DateColumn(format="MM-DD (ddd)", disabled=True),
                    # FORCE 24H Format here
                    "Start": st.column_config.TimeColumn("Act Start", format="HH:mm", step=15),
                    "End": st.column_config.TimeColumn("Act End", format="HH:mm", step=15),
                    # We keep Leave TYPE (sick/annual) but remove Leave HOURS
                    "Leave_Type": st.column_config.SelectboxColumn("Leave Type (if gap)", options=["Annual", "Sick", "Credit", "Comp", "LWOP"]),
                    "OJTI": st.column_config.NumberColumn("OJTI (Hrs)"),
                    "CIC": st.column_config.NumberColumn("CIC (Hrs)")
                }
            )
            
            if st.button("💾 Calculate"):
                models.save_timesheet_v2(pe, edited)
                
                # Fetch Schedule for Holiday Logic
                conn = models.get_db()
                std_sched = pd.read_sql("SELECT * FROM user_schedule", conn).set_index('day_of_week')
                conn.close()
                
                # --- A. Run Time Engine ---
                bucket_rows = []
                for _, row in edited.iterrows():
                    # Pass the Standard Schedule to the logic engine
                    b = logic.calculate_daily_breakdown(
                        row['Date'], row['Start'], row['End'], row['Leave_Type'], 
                        row['OJTI'], row['CIC'], std_sched
                    )
                    bucket_rows.append(b)
                
                # Create DataFrame with all columns
                buckets = pd.DataFrame(bucket_rows, columns=["Regular", "Overtime", "Night", "Sunday", "Holiday", "Hol_Leave", "OJTI", "CIC"])
                
                # --- B. Run Pay Engine ---
                ref_rate, ref_ded, ref_earn = models.get_reference_data(sel_id)
                exp_data = logic.calculate_expected_pay(buckets, ref_rate, act_data['stub'], ref_ded, act_data['leave'], ref_earn)
                
                st.session_state['res'] = exp_data

        # 4. Render
        exp_data = st.session_state.get('res', None)
        if not exp_data:
            # First load default run
            ref_rate, ref_ded, ref_earn = models.get_reference_data(sel_id)
            empty_buckets = pd.DataFrame(columns=["Regular", "Overtime", "Night", "Sunday", "Holiday", "Hol_Leave", "OJTI", "CIC"])
            exp_data = logic.calculate_expected_pay(empty_buckets, ref_rate, act_data['stub'], ref_ded, act_data['leave'], ref_earn)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown('<div class="comp-col comp-expected"><h3 style="background-color: #2e86c1;">🟦 Your Calculation</h3></div>', unsafe_allow_html=True)
            st.markdown(views.render_paystub_html(exp_data, mode="expected"), unsafe_allow_html=True)
        with c2:
            st.markdown('<div class="comp-col comp-actual"><h3 style="background-color: #27ae60;">🟩 Official Paystub</h3></div>', unsafe_allow_html=True)
            st.markdown(views.render_paystub_html(act_data, act_flags, mode="actual"), unsafe_allow_html=True)
    else:
        st.warning("No data.")

with tab_graphs:
    st.header("Financial Trends")
    stubs = models.get_paystubs_meta()
    if not stubs.empty:
        st.line_chart(stubs.set_index('pay_date')[['gross_pay', 'net_pay']])

with tab_ingest:
    if st.button("Scan PayStubs"):
        os.system("python3 ingest.py")
        st.success("Scan processed.")
