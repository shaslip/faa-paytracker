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
    st.info("Enter times as HH:MM (e.g. 07:00 or 15:30). Leave empty for RDOs.")
    
    # Load current schedule
    conn = models.get_db()
    sched_df = pd.read_sql("SELECT * FROM user_schedule ORDER BY day_of_week", conn)
    conn.close()
    
    # --- SIMPLIFICATION: Keep as Strings for Text Editor ---
    # We do NOT convert to datetime objects anymore.
    # We just ensure None/NaN becomes an empty string for the editor if needed, 
    # but Streamlit handles None in TextColumn well.
    
    # Map integers 0-6 to Monday-Sunday
    days_map = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}
    sched_df['Day'] = sched_df['day_of_week'].map(days_map)
    
    # Reorder
    sched_df = sched_df[['Day', 'start_time', 'end_time', 'day_of_week']]
    
    edited_sched = st.data_editor(
        sched_df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "day_of_week": None, 
            "Day": st.column_config.TextColumn(disabled=True),
            # FORCE MILITARY TIME via Regex Validation
            "start_time": st.column_config.TextColumn(
                "Std Start", 
                placeholder="07:00",
                validate="^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$" 
            ),
            "end_time": st.column_config.TextColumn(
                "Std End", 
                placeholder="15:00",
                validate="^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$"
            )
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
    
    # 1. Select Stub
    stubs = models.get_paystubs_meta()
    
    if not stubs.empty:
        # Pre-calculate statuses
        status_map = {}
        for _, row in stubs.iterrows():
            sid = row['id']
            d = models.get_full_paystub_data(sid)
            f = logic.run_full_audit(d)
            status_map[sid] = "🔴" if f else "✅"

        def fmt(rid): 
            r = stubs[stubs['id']==rid].iloc[0]
            icon = status_map.get(rid, "")
            return f"{icon} {r['period_ending']} (Net: ${r['net_pay']:,.2f})"

        sel_id = st.selectbox("Select Pay Period:", stubs['id'], format_func=fmt)
        
        # 2. Get Actuals
        act_data = models.get_full_paystub_data(sel_id)
        act_flags = logic.run_full_audit(act_data)
        pe = act_data['stub']['period_ending']

        # 3. V2 Editor
        with st.expander("📝 Edit Schedule (Actual Worked)", expanded=True):
            # Load V2 timesheet (returns strings now)
            ts_v2 = models.load_timesheet_v2(pe)
            
            edited = st.data_editor(
                ts_v2, 
                num_rows="fixed", 
                hide_index=True,
                column_config={
                    "Date": st.column_config.DateColumn(format="MM-DD (ddd)", disabled=True),
                    # FORCE MILITARY TIME via Regex
                    "Start": st.column_config.TextColumn(
                        "Act Start", 
                        placeholder="07:00",
                        validate="^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$"
                    ),
                    "End": st.column_config.TextColumn(
                        "Act End", 
                        placeholder="15:00",
                        validate="^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$"
                    ),
                    "Leave_Type": st.column_config.SelectboxColumn("Leave Type (if gap)", options=["Annual", "Sick", "Credit", "Comp", "LWOP"]),
                    "OJTI": st.column_config.NumberColumn("OJTI (Hrs)"),
                    "CIC": st.column_config.NumberColumn("CIC (Hrs)")
                }
            )
            
            if st.button("💾 Calculate"):
                models.save_timesheet_v2(pe, edited)
                
                # Fetch Schedule (Strings)
                conn = models.get_db()
                std_sched = pd.read_sql("SELECT * FROM user_schedule", conn).set_index('day_of_week')
                conn.close()
                
                # --- A. Run Time Engine ---
                bucket_rows = []
                for _, row in edited.iterrows():
                    # We must manually convert Strings -> Time Objects for logic.py, 
                    # because logic.py expects objects for math.
                    
                    s_obj = pd.to_datetime(row['Start'], format='%H:%M').time() if row['Start'] else None
                    e_obj = pd.to_datetime(row['End'], format='%H:%M').time() if row['End'] else None
                    
                    b = logic.calculate_daily_breakdown(
                        row['Date'], s_obj, e_obj, row['Leave_Type'], 
                        row['OJTI'], row['CIC'], std_sched
                    )
                    bucket_rows.append(b)
                
                buckets = pd.DataFrame(bucket_rows, columns=["Regular", "Overtime", "Night", "Sunday", "Holiday", "Hol_Leave", "OJTI", "CIC"])
                
                # --- B. Run Pay Engine ---
                ref_rate, ref_ded, ref_earn = models.get_reference_data(sel_id)
                exp_data = logic.calculate_expected_pay(buckets, ref_rate, act_data['stub'], ref_ded, act_data['leave'], ref_earn)
                
                st.session_state['res'] = exp_data

        # 4. Render
        exp_data = st.session_state.get('res', None)
        if not exp_data:
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
