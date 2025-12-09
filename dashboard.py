import streamlit as st
import pandas as pd
import models
import logic
import views
import os

st.set_page_config(page_title="FAA PayTracker", layout="wide")
st.markdown(views.get_css(), unsafe_allow_html=True)
models.setup_database()

tab_audit, tab_graphs, tab_ingest = st.tabs(["🧐 Audit & Time", "📊 Graphs", "📥 Ingestion"])

with tab_audit:
    st.header("Deep Dive Audit")
    
    # 1. Select Stub
    stubs = models.get_paystubs_meta()
    if not stubs.empty:
        # Generate Status Icons
        status_map = {}
        for sid in stubs['id']:
            d = models.get_full_paystub_data(sid)
            f = logic.run_full_audit(d)
            status_map[sid] = "🔴" if f else "✅"

        def fmt(rid): 
            r = stubs[stubs['id']==rid].iloc[0]
            return f"{status_map.get(rid,'')} {r['period_ending']} (Net: ${r['net_pay']:,.2f})"

        sel_id = st.selectbox("Select Pay Period:", stubs['id'], format_func=fmt)
        
        # 2. Get Actuals & Reference
        act_data = models.get_full_paystub_data(sel_id)
        act_flags = logic.run_full_audit(act_data)
        pe = act_data['stub']['period_ending']

        # 3. V2 Editor (Start/End Times)
        with st.expander("📝 Edit Schedule (Time & Attendance)", expanded=True):
            ts_v2 = models.load_timesheet_v2(pe)
            edited = st.data_editor(ts_v2, num_rows="fixed", hide_index=True,
                                    column_config={"Start": st.column_config.TimeColumn(format="HH:mm", step=15),
                                                   "End": st.column_config.TimeColumn(format="HH:mm", step=15)})
            
            if st.button("💾 Calculate"):
                models.save_timesheet_v2(pe, edited)
                
                # A. Run Time Engine
                buckets = pd.DataFrame()
                for _, row in edited.iterrows():
                    b = logic.calculate_daily_breakdown(row['Date'], row['Start'], row['End'], 
                                                        row['Leave'], row['OJTI'], row['CIC'])
                    buckets = pd.concat([buckets, pd.DataFrame([{
                        "Regular": b['Reg'], "Overtime": b['OT'], "Night": b['Night'], 
                        "Sunday": b['Sun'], "Holiday": b['Hol'], "OJTI": b['OJTI'], "CIC": b['CIC']
                    }])], ignore_index=True)
                
                # B. Run Pay Engine
                ref_rate, ref_ded, ref_earn = models.get_reference_data(sel_id)
                exp_data = logic.calculate_expected_pay(buckets, ref_rate, act_data['stub'], ref_ded, act_data['leave'], ref_earn)
                
                st.session_state['res'] = exp_data

        # 4. Render
        exp_data = st.session_state.get('res', None)
        if not exp_data:
            # First load default run
            ref_rate, ref_ded, ref_earn = models.get_reference_data(sel_id)
            exp_data = logic.calculate_expected_pay(pd.DataFrame(), ref_rate, act_data['stub'], ref_ded, act_data['leave'], ref_earn)

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
