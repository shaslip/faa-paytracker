import pandas as pd
import models
from datetime import datetime, timedelta

# --- 1. Create a ledger to track missed government payments ---
def generate_shutdown_ledger(stubs_meta, ref_rate, ref_ded, ref_earn, std_sched):
    ledger = []
    running_balance = 0.0
    
    sorted_stubs = stubs_meta.sort_values('period_ending', ascending=True)

    for _, stub in sorted_stubs.iterrows():
        pe = stub['period_ending']
        
        # Check if the user has explicitly SAVED a timesheet for this period
        is_audited = models.has_saved_timesheet(pe) 

        if not is_audited:
            # OPTION 1: The "Ignorance is Bliss" Approach
            # If you didn't input data, we assume the Gov is correct.
            expected_gross = stub['gross_pay']
            diff = 0.0
            status = "⚪ Unaudited"
            
        else:
            # You entered data -> We trust YOU.
            ts_v2 = models.load_timesheet_v2(pe)
            
            # --- FIX STARTS HERE: Explicitly build the buckets ---
            bucket_rows = []
            for _, row in ts_v2.iterrows():
                # Convert string times to datetime objects if they exist
                s_obj = pd.to_datetime(row['Start'], format='%H:%M').time() if row['Start'] else None
                e_obj = pd.to_datetime(row['End'], format='%H:%M').time() if row['End'] else None
                
                b = calculate_daily_breakdown(
                    row['Date'], s_obj, e_obj, row['Leave_Type'], 
                    row['OJTI'], row['CIC'], std_sched
                )
                bucket_rows.append(b)
            
            # Create the DataFrame that was missing previously
            buckets = pd.DataFrame(bucket_rows)
            # --- FIX ENDS HERE ---

            # Calculate Expected Gross
            # We pass empty dfs for deducs/leave because we only care about Gross for the ledger
            exp_data = calculate_expected_pay(buckets, ref_rate, stub, pd.DataFrame(), pd.DataFrame(), ref_earn)
            expected_gross = exp_data['stub']['gross_pay']
            
            # Calculate Difference
            diff = stub['gross_pay'] - expected_gross
            status = "✅ Balanced"
            
            # Floating point tolerance
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
    if schedule_df.loc[wd, 'is_workday']:
        return date_obj

    # 2. If holiday falls on RDO:
    # Rule: If Sunday (6), slide forward to next workday. 
    #       If any other day (usually Sat), slide back to previous workday.
    offset = 1
    direction = 1 if wd == 6 else -1
    
    while True:
        check_date = date_obj + timedelta(days=(offset * direction))
        # If we slid into a workday, that's the observed holiday
        if schedule_df.loc[check_date.weekday(), 'is_workday']:
            return check_date
        offset += 1

def calculate_daily_breakdown(date_str, act_start, act_end, leave_type, ojti, cic, std_sched_df):
    # --- CONSTANTS ---
    HOLIDAYS = ["2024-12-25", "2025-01-01", "2025-01-20", "2025-02-17", "2025-05-26", 
                "2025-06-19", "2025-07-04", "2025-09-01", "2025-10-13", "2025-11-11", 
                "2025-11-27", "2025-12-25"]

    # --- SETUP ---
    dt_obj = datetime.strptime(date_str, "%Y-%m-%d")
    current_date = dt_obj.date()
    wd = dt_obj.weekday()
    
    # 1. Determine "Observed Holiday" (ATC Slide Rule)
    is_observed_holiday = False
    
    # We iterate through the raw holiday list to see if TODAY is the observed date for any of them
    for h_str in HOLIDAYS:
        h_date = datetime.strptime(h_str, "%Y-%m-%d").date()
        
        # LOGIC:
        # If Holiday falls on Workday -> Observed on that day.
        # If Holiday falls on RDO (Sunday/6) -> Slide Forward +1.
        # If Holiday falls on RDO (Other) -> Slide Back -1.
        
        h_wd = h_date.weekday()
        obs_date = h_date # Default
        
        if not std_sched_df.loc[h_wd, 'is_workday']:
            offset = 1
            direction = 1 if h_wd == 6 else -1
            while True:
                check_date = h_date + timedelta(days=(offset * direction))
                # Check if this new date is a workday
                if std_sched_df.loc[check_date.weekday(), 'is_workday']:
                    obs_date = check_date
                    break
                offset += 1
        
        if obs_date == current_date:
            is_observed_holiday = True
            break

    # 2. Get Standard Expectation (The "Baseline")
    std_row = std_sched_df.loc[wd]
    is_workday = std_row['is_workday']
    std_hours = 0.0
    
    if is_workday and std_row['start_time'] and std_row['end_time']:
        s_std = datetime.strptime(std_row['start_time'], "%H:%M")
        e_std = datetime.strptime(std_row['end_time'], "%H:%M")
        if e_std <= s_std: e_std += timedelta(days=1)
        std_hours = (e_std - s_std).total_seconds() / 3600.0

    # 3. Calculate Actual Worked Data
    worked_hours = 0.0
    night = 0.0
    sun = 0.0
    
    if act_start and act_end:
        s_act = datetime.combine(current_date, act_start)
        e_act = datetime.combine(current_date, act_end)
        
        # Handle crossing midnight
        if e_act <= s_act: e_act += timedelta(days=1)
        
        worked_hours = (e_act - s_act).total_seconds() / 3600.0
        
        # Night Differential Logic (18:00 - 06:00)
        cursor = s_act
        while cursor < e_act:
            if cursor.hour >= 18 or cursor.hour < 6: 
                night += 0.25
            cursor += timedelta(minutes=15)
            
        # Sunday Premium Logic (Touch Rule)
        if s_act.weekday() == 6 or e_act.weekday() == 6:
            sun = min(8.0, worked_hours)

    # 4. The Gap Analysis (Leave Calculation)
    leave_hours_charged = 0.0
    hol_leave_hours = 0.0
    
    # Only calculate gaps if this is a SCHEDULED Workday
    if is_workday:
        gap = max(0.0, std_hours - worked_hours)
        
        if gap > 0:
            if leave_type == "Holiday" or is_observed_holiday:
                # It is a paid holiday gap (Free)
                hol_leave_hours = gap
            else:
                # Normal leave charged
                leave_hours_charged = gap
    
    # 5. Buckets for Pay Calculation
    reg = 0.0
    ot = 0.0
    
    if is_workday:
        # Normal Workday or Observed Holiday:
        # First 8 hours are Regular Pay (or covered by Hol Leave if gap)
        # Anything over 8 is OT.
        reg = min(8.0, worked_hours)
        ot = max(0.0, worked_hours - 8.0)
    else:
        # RDO (Regular Day Off):
        # If you work on an RDO, ALL hours are Overtime.
        # (Even if this was the 'Calendar Holiday', the slide rule moved the holiday status away)
        reg = 0.0
        ot = worked_hours

    # Holiday Worked Premium
    # You get this ONLY if working on the OBSERVED Holiday.
    hol_worked_premium = 0.0
    if is_observed_holiday and worked_hours > 0:
        hol_worked_premium = min(8.0, worked_hours)

    # Return Dictionary
    return {
        "Regular": reg, 
        "Overtime": ot, 
        "Night": night, 
        "Sunday": sun, 
        "Holiday": hol_worked_premium,   # Premium Pay (worked)
        "Hol_Leave": hol_leave_hours,    # Base Pay (not worked)
        "Leave_Hrs": leave_hours_charged,# User charged leave (Annual/Sick)
        "Leave_Type": leave_type if leave_hours_charged > 0 else None,
        "OJTI": ojti, 
        "CIC": cic
    }

# --- 4. Paycheck Calculator (FLSA Weighted Average) ---
def calculate_expected_pay(buckets_df, base_rate, actual_meta, ref_deductions, actual_leave, ref_earnings):
    # --- 1. Calculate Earnings Amounts (Same as before) ---
    t_reg = buckets_df['Regular'].sum()
    t_ot = buckets_df['Overtime'].sum()
    t_night = buckets_df['Night'].sum()
    t_sun = buckets_df['Sunday'].sum()
    t_hol_work = buckets_df['Holiday'].sum() 
    t_hol_leave = buckets_df.get('Hol_Leave', pd.Series(0)).sum() if 'Hol_Leave' in buckets_df else 0.0
    t_ojti = buckets_df['OJTI'].sum()
    t_cic = buckets_df['CIC'].sum()

    # Base Amounts
    amt_reg = round(t_reg * base_rate, 2)
    amt_hol_leave = round(t_hol_leave * base_rate, 2)    
    amt_true_ot = round(t_ot * base_rate, 2) if t_ot > 0 else 0.0
    
    # Differentials
    r_night = round(base_rate * 0.10, 2); amt_night = round(t_night * r_night, 2)
    r_sun = round(base_rate * 0.25, 2); amt_sun = round(t_sun * r_sun, 2)
    amt_hol = round(t_hol_work * base_rate, 2)
    r_ojti = round(base_rate * 0.10, 2); amt_ojti = round(t_ojti * r_ojti, 2)
    r_cic = round(base_rate * 0.10, 2); amt_cic = round(t_cic * r_cic, 2)
    
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
                 amt_cip = round((amt_reg + amt_hol_leave) * factor, 2)
                 r_cip = round(base_rate * factor, 2)

    # FLSA Calculation
    amt_flsa = 0.0; r_flsa = 0.0
    if t_ot > 0:
        remun = amt_reg + amt_true_ot + amt_night + amt_sun + amt_hol + amt_cip + amt_ojti + amt_cic
        hrs = t_reg + t_hol_leave + t_ot
        if hrs > 0:
            rrp = remun / hrs
            r_flsa = round(rrp * 0.5, 2)
            amt_flsa = round(t_ot * r_flsa, 2)

    # Calculate Gross
    gross = amt_reg + amt_hol_leave + amt_true_ot + amt_flsa + amt_night + amt_sun + amt_hol + amt_cip + amt_ojti + amt_cic
    
    # --- 2. Dynamic Deductions (With YTD Fix) ---
    deduction_rows = []
    total_deducs = 0.0
    PERCENTAGE_BASED = ['Federal Tax', 'State Tax', 'OASDI', 'Medicare', 'FERS', 'TSP'] 

    ref_gross = ref_earnings['amount_current'].sum() if not ref_earnings.empty else 1.0
    
    if not ref_deductions.empty:
        for _, row in ref_deductions.iterrows():
            d_type = row['type']
            ref_amt = row['amount_current']
            ref_ytd = row.get('amount_ytd', 0.0)
            
            # A. Calculate New Current
            new_amt = ref_amt 
            is_variable = any(x in d_type for x in PERCENTAGE_BASED)
            if is_variable and ref_gross > 0:
                effective_rate = ref_amt / ref_gross
                new_amt = round(gross * effective_rate, 2)
            
            # B. Calculate New YTD (Swap out the ref amount for the new amount)
            # Formula: Old YTD - Old Current + New Current
            new_ytd = round(ref_ytd - ref_amt + new_amt, 2)

            deduction_rows.append({
                'type': d_type,
                'amount_current': new_amt,
                'amount_ytd': new_ytd,
                'code': row.get('code', '')
            })
            total_deducs += new_amt

    d_df = pd.DataFrame(deduction_rows)
    net = gross - total_deducs

    # --- 3. Build Earnings Rows (With YTD Fix) ---
    # Helper to find reference data for YTD calc
    def get_ref_ytd(type_name, new_current):
        if ref_earnings.empty: return new_current
        # Fuzzy match the type name
        match = ref_earnings[ref_earnings['type'].str.contains(type_name, case=False, regex=False, na=False)]
        if not match.empty:
            r_ytd = match.iloc[0]['amount_ytd']
            r_curr = match.iloc[0]['amount_current']
            return round(r_ytd - r_curr + new_current, 2)
        return new_current # If new type, YTD is just the current amount

    rows = []
    
    # List of tuples: (Type, Rate, Hours, Amount)
    # We use specific keywords that match typical paystub labels to help the helper function find matches
    items = []
    if t_reg > 0: items.append(("Regular Pay", base_rate, t_reg, amt_reg))
    if t_hol_leave > 0: items.append(("Holiday Leave", base_rate, t_hol_leave, amt_hol_leave))
    if amt_cip: items.append(("Controller Incentive Pay", r_cip, (t_reg + t_hol_leave), amt_cip))
    
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

    # --- 4. Leave Recalc (Unchanged) ---
    l_rows = []
    target_leaves = ['Annual', 'Sick', 'Credit']
    if not actual_leave.empty:
        for _, row in actual_leave.iterrows():
            if any(x in row['type'] for x in target_leaves):
                end = row['balance_start'] + row['earned_current']
                l_rows.append({
                    'type': row['type'], 'balance_start': row['balance_start'],
                    'earned_current': row['earned_current'], 'used_current': 0.0, 'balance_end': end
                })
    l_df = pd.DataFrame(l_rows)

    stub = actual_meta.copy()
    stub.update({'gross_pay': gross, 'net_pay': net, 'total_deductions': total_deducs, 'remarks': 'GENERATED V2\nWeighted Avg FLSA'})
    
    return {'stub': stub, 'earnings': e_df, 'deductions': d_df, 'leave': l_df}
