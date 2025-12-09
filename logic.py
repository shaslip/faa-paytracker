import pandas as pd
from datetime import datetime, timedelta

# --- 1. Audit Math (Leave & Gross/Net) ---
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

# --- 2. Time Engine (V2) ---

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
"""
    1. Looks up Standard Schedule for this day (e.g., 8 hours).
    2. Calculates Actual Worked hours.
    3. Difference = Leave Hours (billed as 'leave_type').
    """
    dt_obj = datetime.strptime(date_str, "%Y-%m-%d")
    wd = dt_obj.weekday()
    
    # Get Standard Expectation
    std_row = std_sched_df.loc[wd]
    is_workday = std_row['is_workday']
    
    # 1. Calculate Standard Duration (Expected Hours)
    std_hours = 0.0
    if is_workday and std_row['start_time'] and std_row['end_time']:
        s_std = datetime.strptime(std_row['start_time'], "%H:%M")
        e_std = datetime.strptime(std_row['end_time'], "%H:%M")
        if e_std <= s_std: e_std += timedelta(days=1)
        std_hours = (e_std - s_std).total_seconds() / 3600.0
    
    # 2. Calculate Actual Worked
    worked_hours = 0.0
    night = 0.0
    sun = 0.0
    
    if act_start and act_end:
        s_act = datetime.combine(dt_obj.date(), act_start)
        e_act = datetime.combine(dt_obj.date(), act_end)
        if e_act <= s_act: e_act += timedelta(days=1)
        worked_hours = (e_act - s_act).total_seconds() / 3600.0
        
        # Calculate Diffs based on ACTUAL time worked (More Accurate!)
        cursor = s_act
        while cursor < e_act:
            if cursor.hour >= 18 or cursor.hour < 6: night += 0.25
            cursor += timedelta(minutes=15)
            
        # Sunday Premium (Touch rule on Actuals)
        if s_act.weekday() == 6 or e_act.weekday() == 6:
            # Note: Sunday premium is generally capped at 8 hours or the shift duration
            sun = min(8.0, worked_hours)

    # 3. Calculate Leave (The Gap)
    leave_hours = 0.0
    # Only calculate leave gap if it's a workday and we worked LESS than standard
    if is_workday:
        leave_hours = max(0.0, std_hours - worked_hours)
    
    # 4. Buckets
    reg = min(8.0, worked_hours)
    ot = max(0.0, worked_hours - 8.0)
    
    # If we have leave hours but no type selected, default to 'Annual' or flag it?
    # For now we just return the hours.
    
    # Handle Holiday Logic (Slide Rule) here if needed...
    # (Omitting for brevity, but insert the Slide Rule logic from previous step here)

    return {
        "Regular": reg, 
        "Overtime": ot, 
        "Night": night, 
        "Sunday": sun, 
        "Leave_Hrs": leave_hours, # Logic engine uses this to bill base pay
        "Leave_Type": leave_type if leave_hours > 0 else None,
        "OJTI": ojti, 
        "CIC": cic
    }

# --- 3. Paycheck Calculator (FLSA Weighted Average) ---
def calculate_expected_pay(buckets_df, base_rate, actual_meta, ref_deductions, actual_leave, ref_earnings):
    # Sum buckets
    t_reg = buckets_df['Regular'].sum()
    t_ot = buckets_df['Overtime'].sum()
    t_night = buckets_df['Night'].sum()
    t_sun = buckets_df['Sunday'].sum()
    t_hol_work = buckets_df['Holiday'].sum() # Premium
    t_hol_leave = buckets_df.get('Hol_Leave', pd.Series(0)).sum() if 'Hol_Leave' in buckets_df else 0.0
    t_ojti = buckets_df['OJTI'].sum()
    t_cic = buckets_df['CIC'].sum()

    # 1. Base Amounts (Reg + Holiday Leave are both paid at Base Rate)
    # Note: 'Regular' bucket usually implies worked hours. Holiday Leave adds to the base pay hours.
    amt_reg = round((t_reg + t_hol_leave) * base_rate, 2)
    
    amt_true_ot = round(t_ot * base_rate, 2) if t_ot > 0 else 0.0
    
    # 2. Differentials
    r_night = round(base_rate * 0.10, 2); amt_night = round(t_night * r_night, 2)
    r_sun = round(base_rate * 0.25, 2); amt_sun = round(t_sun * r_sun, 2)
    
    # Holiday Worked Premium (Usually 100% of base rate, so effectively 2x total)
    amt_hol = round(t_hol_work * base_rate, 2)
    
    # 3. Dynamic Diffs (OJTI/CIC/CIP)
    r_ojti = round(base_rate * 0.10, 2); amt_ojti = round(t_ojti * r_ojti, 2)
    r_cic = round(base_rate * 0.10, 2); amt_cic = round(t_cic * r_cic, 2)
    
    # CIP Logic
    amt_cip = 0.0; r_cip = 0.0
    if not ref_earnings.empty:
         cip_row = ref_earnings[ref_earnings['type'].str.contains('Controller Incentive', case=False)]
         reg_row = ref_earnings[ref_earnings['type'].str.contains('Regular', case=False)]
         if not cip_row.empty and not reg_row.empty:
             hist_cip = cip_row.iloc[0]['amount_current']
             hist_reg = reg_row.iloc[0]['amount_current']
             if hist_reg > 0:
                 factor = hist_cip / hist_reg
                 # CIP applies to Basic Pay (Reg + Hol Leave)
                 amt_cip = round(amt_reg * factor, 2)
                 r_cip = round(base_rate * factor, 2)

    # 4. FLSA Calculation (Weighted Average)
    amt_flsa = 0.0; r_flsa = 0.0
    if t_ot > 0:
        # Numerator: Base (Reg+HolLeave) + True OT + Diffs + CIP + OJTI + CIC
        # Note: Holiday Premium is included in FLSA calc
        remun = amt_reg + amt_true_ot + amt_night + amt_sun + amt_hol + amt_cip + amt_ojti + amt_cic
        hrs = t_reg + t_hol_leave + t_ot
        if hrs > 0:
            rrp = remun / hrs
            r_flsa = round(rrp * 0.5, 2)
            amt_flsa = round(t_ot * r_flsa, 2)

    # 5. Totals
    gross = amt_reg + amt_true_ot + amt_flsa + amt_night + amt_sun + amt_hol + amt_cip + amt_ojti + amt_cic
    deducs = ref_deductions['amount_current'].sum() if not ref_deductions.empty else 0.0
    net = gross - deducs

    # 6. Build Rows
    rows = []
    if (t_reg + t_hol_leave) > 0: 
        rows.append(["Regular / Holiday Leave", base_rate, (t_reg + t_hol_leave), amt_reg])
        
    if amt_cip: rows.append(["Controller Incentive Pay", r_cip, (t_reg + t_hol_leave), amt_cip])
    if t_ot:
        rows.append(["FLSA Premium", r_flsa, t_ot, amt_flsa])
        rows.append(["True Overtime", base_rate, t_ot, amt_true_ot])
    if t_night: rows.append(["Night Differential", r_night, t_night, amt_night])
    if t_sun: rows.append(["Sunday Premium", r_sun, t_sun, amt_sun])
    if t_hol_work: rows.append(["Holiday Worked", base_rate, t_hol_work, amt_hol])
    if t_ojti: rows.append(["OJTI", r_ojti, t_ojti, amt_ojti])
    if t_cic: rows.append(["CIC", r_cic, t_cic, amt_cic])
    
    e_df = pd.DataFrame(rows, columns=['type', 'rate', 'hours_current', 'amount_current'])
    e_df['amount_ytd'] = 0.0; e_df['hours_adjusted'] = 0.0; e_df['amount_adjusted'] = 0.0

    # 7. Leave Recalc (Start + Earned - Used)
    l_rows = []
    target_leaves = ['Annual', 'Sick', 'Credit']
    if not actual_leave.empty:
        for _, row in actual_leave.iterrows():
            if any(x in row['type'] for x in target_leaves):
                end = row['balance_start'] + row['earned_current'] # Used is 0 in this V2 model for now
                l_rows.append({
                    'type': row['type'], 'balance_start': row['balance_start'],
                    'earned_current': row['earned_current'], 'used_current': 0.0, 'balance_end': end
                })
    l_df = pd.DataFrame(l_rows)

    stub = actual_meta.copy()
    stub.update({'gross_pay': gross, 'net_pay': net, 'total_deductions': deducs, 'remarks': 'GENERATED V2\nWeighted Avg FLSA'})
    
    return {'stub': stub, 'earnings': e_df, 'deductions': ref_deductions, 'leave': l_df}
