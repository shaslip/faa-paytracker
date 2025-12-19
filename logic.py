import pandas as pd
import models
import math
import json
import os
from datetime import datetime, timedelta

def load_holidays():
    """Loads holidays from holidays.json located in the same directory."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(script_dir, "holidays.json")
    
    if os.path.exists(json_path):
        with open(json_path, 'r') as f:
            return json.load(f)
    return {}

# --- 1. Create a ledger to track missed government payments ---
def generate_shutdown_ledger(stubs_meta, ref_rate, ref_ded, ref_earn, std_sched_ignored):
    ledger = []
    running_balance = 0.0
    
    sorted_stubs = stubs_meta.sort_values('period_ending', ascending=True)

    for _, stub in sorted_stubs.iterrows():
        pe = stub['period_ending']
        
        # 1. Fetch Historical Context (Rate & Schedule) for THIS specific year
        cur_rate, cur_ded, cur_earn = models.get_reference_data(stub['id'])
        
        pe_date = datetime.strptime(pe, "%Y-%m-%d")
        # Fetch the schedule specific to this paystub's year
        # We ignore the 'std_sched_ignored' argument passed from dashboard
        hist_sched = models.get_user_schedule(pe_date.year).set_index('day_of_week')

        # Check if the user has explicitly SAVED a timesheet for this period
        is_audited = models.has_saved_timesheet(pe) 

        if not is_audited:
            # OPTION 1: The "Ignorance is Bliss" Approach
            expected_gross = stub['gross_pay']
            diff = 0.0
            status = "⚪ Unaudited"
            
        else:
            # You entered data -> We trust YOU.
            ts_v2 = models.load_timesheet_v2(pe)
            
            bucket_rows = []
            for _, row in ts_v2.iterrows():
                # Convert string times to datetime objects if they exist
                s_raw = row['Start']
                e_raw = row['End']
                
                s_obj = pd.to_datetime(s_raw, format='%H:%M').time() if s_raw and s_raw != "None" else None
                e_obj = pd.to_datetime(e_raw, format='%H:%M').time() if e_raw and e_raw != "None" else None
                
                # Pass the HISTORICAL schedule to the breakdown
                b = calculate_daily_breakdown(
                    row['Date'], s_obj, e_obj, row['Leave_Type'], 
                    row['OJTI'], row['CIC'], hist_sched
                )
                bucket_rows.append(b)
            
            # Create DataFrame with explicit columns to prevent empty-list crashes
            cols = ["Regular", "Overtime", "Night", "Sunday", "Holiday", "Hol_Leave", "OJTI", "CIC"]
            buckets = pd.DataFrame(bucket_rows, columns=cols)
            buckets = buckets.fillna(0.0)

            # Calculate Expected Gross using the HISTORICAL rate
            exp_data = calculate_expected_pay(buckets, cur_rate, stub, pd.DataFrame(), pd.DataFrame(), cur_earn)
            expected_gross = exp_data['stub']['gross_pay']
            
            # Calculate Difference
            diff = stub['gross_pay'] - expected_gross
            status = "✅ Balanced"
            
            if diff < -1.0: status = "🔴 Gov Owes You"
            elif diff > 1.0: status = "🟢 Backpay/Surplus"

        # Update Running Balance
        running_balance += diff
        
        ledger.append({
            "Period Ending": pe,
            "Expected": expected_gross,
            "Actual": stub['gross_pay'],
            "Diff": diff,
            "Balance": running_balance,
            "Status": status
        })

    return pd.DataFrame(ledger)

# --- 2. Audit Math (Leave & Gross/Net) ---
def run_full_audit(data):
    stub = data['stub']
    flags = {}
    EXEMPT = ["Admin", "Change of Station Leave", "Time Off Award", "Gov Shutdown-Excepted"]
    
    # User Note: 8.50 means 8 hours 50 minutes for Leave
    def to_min(v): 
        if v is None: return 0
        h = int(v); m = round((v-h)*100)
        return (h*60) + m
    def to_dot(m): return (m//60) + ((m%60)/100.0)

    for _, row in data['leave'].iterrows():
        if row['type'] in EXEMPT: continue
        s_min = to_min(row['balance_start'])
        e_min = to_min(row['earned_current'])
        u_min = to_min(row['used_current'])
        end_act = to_min(row['balance_end'])
        
        calc = s_min + e_min - u_min
        if abs(calc - end_act) > 1:
            flags[f"leave_{row['type']}_end"] = f"Math Error: {to_dot(s_min):.2f} + {to_dot(e_min):.2f} - {to_dot(u_min):.2f} should be {to_dot(calc):.2f}, stub says {to_dot(end_act):.2f}"

    calc_gross = data['earnings']['amount_current'].sum() + data['earnings']['amount_adjusted'].sum()
    if abs(calc_gross - stub['gross_pay']) > 0.01:
        flags['gross_pay'] = f"Sum ({calc_gross:,.2f}) != Gross ({stub['gross_pay']:,.2f})"
    if abs((stub['gross_pay'] - stub['total_deductions']) - stub['net_pay']) > 0.01:
        flags['net_pay'] = "Math Error: Gross - Ded != Net"
        
    return flags

# --- 3. Time Engine (V2) ---
def get_observed_holiday(date_obj, schedule_df):
    """
    Determines the 'In-Lieu-Of' date for a given holiday based on RDOs (ATC Slide Rule).
    schedule_df must be indexed by day_of_week (0=Mon, 6=Sun) with 'is_workday' bool.
    """
    wd = date_obj.weekday()
    
    # 1. If holiday falls on a Workday, that is the holiday.
    # Safety: Use .get() or try/except to handle missing schedule rows
    try:
        if schedule_df.loc[wd, 'is_workday']:
            return date_obj
    except (KeyError, IndexError):
        # Fallback if schedule is incomplete: assume standard M-F
        if wd < 5: return date_obj

    # 2. If holiday falls on RDO:
    # Rule: If Sunday (6), slide forward to next workday. 
    #       If any other day (usually Sat), slide back to previous workday.
    offset = 1
    direction = 1 if wd == 6 else -1
    
    # Safety breakout to prevent infinite loops if schedule is empty
    attempts = 0
    while attempts < 14:
        check_date = date_obj + timedelta(days=(offset * direction))
        try:
            if schedule_df.loc[check_date.weekday(), 'is_workday']:
                return check_date
        except (KeyError, IndexError):
            pass # Keep looking
            
        offset += 1
        attempts += 1
    
    return date_obj # Fallback

def calculate_daily_breakdown(date_str, act_start, act_end, leave_type, ojti, cic, std_sched_df):
    # --- SETUP ---
    dt_obj = datetime.strptime(date_str, "%Y-%m-%d")
    current_date = dt_obj.date()
    wd = dt_obj.weekday()
    
    # 1. Determine "Observed Holiday" using JSON loader
    all_holidays = load_holidays()
    year_str = str(dt_obj.year)
    year_holidays = all_holidays.get(year_str, [])
    
    is_observed_holiday = False
    for h_str in year_holidays:
        h_date = datetime.strptime(h_str, "%Y-%m-%d").date()
        obs_date = get_observed_holiday(h_date, std_sched_df)
        if obs_date == current_date:
            is_observed_holiday = True
            break

    # 2. Get Standard Expectation (Check if RDO or Workday)
    # Handle cases where std_sched_df might be missing this day (Year mismatch fallback)
    is_workday = False
    std_hours = 0.0
    
    if wd in std_sched_df.index:
        std_row = std_sched_df.loc[wd]
        is_workday = std_row['is_workday']
        
        if is_workday and std_row['start_time'] and std_row['end_time']:
            s_std = datetime.strptime(std_row['start_time'], "%H:%M")
            e_std = datetime.strptime(std_row['end_time'], "%H:%M")
            if e_std <= s_std: e_std += timedelta(days=1)
            std_hours = (e_std - s_std).total_seconds() / 3600.0

    # 3. Calculate Actual Worked Data
    worked_hours = 0.0
    night = 0.0
    final_sunday_premium = 0.0
    
    if act_start is not None and act_end is not None:
        if act_start == act_end:
            worked_hours = 0.0
        else:
            s_act = datetime.combine(current_date, act_start)
            e_act = datetime.combine(current_date, act_end)
            
            # --- INTELLIGENT SHIFT LOGIC (Mid-Shift Heuristic) ---
            if e_act < s_act:
                if s_act.hour >= 19:
                    s_act -= timedelta(days=1)
                else:
                    e_act += timedelta(days=1)
            
            worked_hours = (e_act - s_act).total_seconds() / 3600.0
            
            # --- DIFFERENTIALS ---
            # We track "Calendar Sunday" hours inside the loop for Overtime shifts
            calendar_sunday_hours = 0.0
            
            cursor = s_act
            while cursor < e_act:
                # 1. Night Diff
                if cursor.hour >= 18 or cursor.hour < 6: 
                    night += 0.25
                
                # 2. Calendar Sunday (Strict Accumulation)
                if cursor.weekday() == 6:
                    calendar_sunday_hours += 0.25

                cursor += timedelta(minutes=15)
            
            # --- SUNDAY PREMIUM DECISION LOGIC ---
            if is_workday:
                # REGULAR SHIFT -> Apply Touch Rule
                if s_act.weekday() == 6 or e_act.weekday() == 6:
                    final_sunday_premium = min(8.0, worked_hours)
            else:
                # OVERTIME SHIFT (RDO) -> Apply Calendar Rule
                final_sunday_premium = calendar_sunday_hours

    # 4. The Gap Analysis
    leave_hours_charged = 0.0
    hol_leave_hours = 0.0
    
    if is_workday:
        gap = max(0.0, std_hours - worked_hours)
        if gap > 0:
            if leave_type == "Holiday" or is_observed_holiday:
                hol_leave_hours = gap
            elif leave_type and leave_type != "None":
                leave_hours_charged = gap
            else:
                pass 
    
    # 5. Buckets
    reg = 0.0
    ot = 0.0
    
    if is_workday:
        reg = min(8.0, worked_hours)
        ot = max(0.0, worked_hours - 8.0)
    else:
        reg = 0.0
        ot = worked_hours

    # Holiday Worked Premium
    hol_worked_premium = 0.0
    if is_observed_holiday and worked_hours > 0:
        hol_worked_premium = min(8.0, worked_hours)

    return {
        "Regular": reg, 
        "Overtime": ot, 
        "Night": night, 
        "Sunday": final_sunday_premium,
        "Holiday": hol_worked_premium,
        "Hol_Leave": hol_leave_hours,
        "Leave_Hrs": leave_hours_charged,
        "Leave_Type": leave_type if leave_hours_charged > 0 else None,
        "OJTI": ojti, 
        "CIC": cic
    }

# --- 4. Paycheck Calculator (FLSA Weighted Average) ---
# Payroll System appears to use 4-Decimal Truncation in calculations
def truncate_hours(val):
    """Truncates to 4 decimal places to match legacy payroll systems."""
    return math.floor(val * 10000) / 10000.0

def truncate_cents(val):
    """Truncates to 2 decimal places (cents) to match payroll systems."""
    return math.floor(val * 100) / 100.0
    
def calculate_expected_pay(buckets_df, base_rate, actual_meta, ref_deductions, actual_leave, ref_earnings):
    # Sum buckets (ALL truncated to 4 decimals to match payroll system precision)
    t_reg = truncate_hours(buckets_df['Regular'].sum())
    t_ot = truncate_hours(buckets_df['Overtime'].sum())
    t_night = truncate_hours(buckets_df['Night'].sum())
    t_sun = truncate_hours(buckets_df['Sunday'].sum())
    t_hol_work = truncate_hours(buckets_df['Holiday'].sum())
    
    t_hol_leave = truncate_hours(buckets_df.get('Hol_Leave', pd.Series(0)).sum()) if 'Hol_Leave' in buckets_df else 0.0
    t_ojti = truncate_hours(buckets_df['OJTI'].sum())
    t_cic = truncate_hours(buckets_df['CIC'].sum())
    
    # Aggregate Regular Pay (Worked + Holiday Leave)
    total_reg_hours = t_reg + t_hol_leave
    amt_reg_total = truncate_cents(total_reg_hours * base_rate)
    
    # Base Amounts
    amt_true_ot = truncate_cents(t_ot * base_rate) if t_ot > 0 else 0.0
    
    # Differentials
    r_night = truncate_cents(base_rate * 0.10); amt_night = truncate_cents(t_night * r_night)
    r_sun = truncate_cents(base_rate * 0.25); amt_sun = truncate_cents(t_sun * r_sun)
    amt_hol = truncate_cents(t_hol_work * base_rate)
    r_ojti = truncate_cents(base_rate * 0.25); amt_ojti = truncate_cents(t_ojti * r_ojti)
    r_cic = truncate_cents(base_rate * 0.10); amt_cic = truncate_cents(t_cic * r_cic)
    
    # CIP Logic
    amt_cip = 0.0; r_cip = 0.0
    if not ref_earnings.empty:
         cip_row = ref_earnings[ref_earnings['type'].str.contains('Controller Incentive', case=False, na=False)]
         reg_row = ref_earnings[ref_earnings['type'].str.contains('Regular', case=False, na=False)]
         if not cip_row.empty and not reg_row.empty:
             hist_cip = cip_row.iloc[0]['amount_current']
             hist_reg = reg_row.iloc[0]['amount_current']
             if hist_reg > 0:
                 factor = hist_cip / hist_reg
                 amt_cip = truncate_cents(amt_reg_total * factor)
                 r_cip = truncate_cents(base_rate * factor)
    
    # FLSA Calculation
    amt_flsa = 0.0; r_flsa = 0.0
    if t_ot > 0:
        remun = amt_reg_total + amt_true_ot + amt_night + amt_sun + amt_hol + amt_cip + amt_ojti + amt_cic
        hrs = total_reg_hours + t_ot
        if hrs > 0:
            rrp = remun / hrs
            r_flsa = truncate_cents(rrp * 0.5)
            amt_flsa = truncate_cents(t_ot * r_flsa)
    
    # Calculate Gross & Deductions
    gross = amt_reg_total + amt_true_ot + amt_flsa + amt_night + amt_sun + amt_hol + amt_cip + amt_ojti + amt_cic
    
    deduction_rows = []
    total_deducs = 0.0
    PERCENTAGE_BASED = ['Federal Tax', 'State Tax', 'OASDI', 'Medicare', 'FERS', 'TSP'] 
    ref_gross = ref_earnings['amount_current'].sum() if not ref_earnings.empty else 1.0
    
    if not ref_deductions.empty:
        for _, row in ref_deductions.iterrows():
            d_type = row['type']
            ref_amt = row['amount_current']
            ref_ytd = row.get('amount_ytd', 0.0)
            
            new_amt = ref_amt 
            is_variable = any(x in d_type for x in PERCENTAGE_BASED)
            if is_variable and ref_gross > 0:
                effective_rate = ref_amt / ref_gross
                new_amt = truncate_cents(gross * effective_rate)
            
            # YTD Logic: If ref_ytd is 0, we assume data is missing/invalid, so we show None
            new_ytd = truncate_cents(ref_ytd - ref_amt + new_amt) if ref_ytd > 0 else None
            deduction_rows.append({'type': d_type, 'amount_current': new_amt, 'amount_ytd': new_ytd, 'code': row.get('code', '')})
            total_deducs += new_amt
    d_df = pd.DataFrame(deduction_rows)
    net = gross - total_deducs

    # --- Build Earnings Rows ---
    def get_ref_ytd(type_name, new_current):
        if ref_earnings.empty: return new_current
        # Exact match logic (safer than contains) or fuzzy fallback
        match = ref_earnings[ref_earnings['type'].str.contains(type_name, case=False, regex=False, na=False)]
        
        if not match.empty:
            r_ytd = match.iloc[0]['amount_ytd']
            r_curr = match.iloc[0]['amount_current']
            
            if r_ytd <= 0.01: return None
                
            return round(r_ytd - r_curr + new_current, 2)
            
        return None 

    rows = []
    items = []
    
    if total_reg_hours > 0: 
        items.append(("Regular", base_rate, total_reg_hours, amt_reg_total))
    
    if amt_cip: items.append(("Controller Incentive Pay", r_cip, total_reg_hours, amt_cip))
    
    if t_ot:
        items.append(("FLSA Premium", r_flsa, t_ot, amt_flsa))
        items.append(("True Overtime", base_rate, t_ot, amt_true_ot))
        
    if t_night: items.append(("Night Differential", r_night, t_night, amt_night))
    if t_sun: items.append(("Sunday Premium", r_sun, t_sun, amt_sun))
    if t_hol_work: items.append(("Holiday Worked", base_rate, t_hol_work, amt_hol))
    if t_ojti: items.append(("OJTI", r_ojti, t_ojti, amt_ojti))
    if t_cic: items.append(("CIC", r_cic, t_cic, amt_cic))
    
    for label, rate, hrs, amt in items:
        ytd = get_ref_ytd(label, amt)
        rows.append([label, rate, hrs, amt, ytd])
    
    e_df = pd.DataFrame(rows, columns=['type', 'rate', 'hours_current', 'amount_current', 'amount_ytd'])
    e_df['hours_adjusted'] = 0.0; e_df['amount_adjusted'] = 0.0

    # --- Helper: Convert Decimal Hours to HH:MM String for Display ---
    def fmt_hours(val):
        if not val or val < 0.001: return ""
        total_minutes = int(round(val * 60))
        h = total_minutes // 60
        m = total_minutes % 60
        return f"{h}:{m:02d}"

    e_df['hours_current'] = e_df['hours_current'].apply(fmt_hours)
    # -------------------------------------------------------------

    # Leave Recalc
    l_rows = []
    target_leaves = ['Annual', 'Sick', 'Credit']
    if not actual_leave.empty:
        for _, row in actual_leave.iterrows():
            if any(x in row['type'] for x in target_leaves):
                bal_start = row.get('balance_start', 0.0)
                earned = row.get('earned_current', 0.0)
                end = bal_start + earned
                
                l_rows.append({
                    'type': row['type'], 
                    'balance_start': bal_start,
                    'earned_current': earned, 
                    'used_current': 0.0, 
                    'balance_end': end
                })
    l_df = pd.DataFrame(l_rows)

    stub = actual_meta.copy()
    stub.update({'gross_pay': gross, 'net_pay': net, 'total_deductions': total_deducs, 'remarks': 'GENERATED V2\nWeighted Avg FLSA'})
    
    return {'stub': stub, 'earnings': e_df, 'deductions': d_df, 'leave': l_df}
