import pandas as pd
from datetime import datetime, timedelta

# --- 1. Audit Math (Leave & Gross/Net) ---
def run_full_audit(data):
    stub = data['stub']
    flags = {}
    EXEMPT = ["Admin", "Change of Station Leave", "Time Off Award", "Gov Shutdown-Excepted"]
    
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
def calculate_daily_breakdown(date_str, start_time, end_time, leave_hours, ojti, cic):
    if not start_time or not end_time:
        return {'Reg': 0, 'OT': 0, 'Night': 0, 'Sun': 0, 'Hol': 0, 'OJTI': ojti, 'CIC': cic}

    start_dt = datetime.combine(datetime.strptime(date_str, "%Y-%m-%d").date(), start_time)
    end_dt = datetime.combine(datetime.strptime(date_str, "%Y-%m-%d").date(), end_time)
    if end_dt <= start_dt: end_dt += timedelta(days=1)

    total_dur = (end_dt - start_dt).total_seconds() / 3600.0
    worked = max(0, total_dur - leave_hours)
    
    reg = min(8.0, worked)
    ot = max(0.0, worked - 8.0)
    
    # Night Logic (18:00 - 06:00)
    night = 0.0
    cursor = start_dt
    end_scan = start_dt + timedelta(hours=worked)
    while cursor < end_scan:
        if cursor.hour >= 18 or cursor.hour < 6: night += 0.25
        cursor += timedelta(minutes=15)
        
    # Sunday Logic (Touch Rule)
    sun = 0.0
    if start_dt.weekday() == 6 or end_dt.weekday() == 6:
        sun = reg
        
    # Holiday Logic
    HOLIDAYS = ["2024-12-25", "2025-01-01", "2025-01-20", "2025-02-17", "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-10-13", "2025-11-11", "2025-11-27", "2025-12-25"]
    hol = 0.0
    if date_str in HOLIDAYS:
        hol = reg; reg = 0.0
        
    return {'Reg': reg, 'OT': ot, 'Night': night, 'Sun': sun, 'Hol': hol, 'OJTI': ojti, 'CIC': cic}

# --- 3. Paycheck Calculator (FLSA Weighted Average) ---
def calculate_expected_pay(buckets_df, base_rate, actual_meta, ref_deductions, actual_leave, ref_earnings):
    # Sum buckets
    t_reg = buckets_df['Regular'].sum(); t_ot = buckets_df['Overtime'].sum()
    t_night = buckets_df['Night'].sum(); t_sun = buckets_df['Sunday'].sum()
    t_hol = buckets_df['Holiday'].sum(); t_ojti = buckets_df['OJTI'].sum(); t_cic = buckets_df['CIC'].sum()

    # 1. Base Amounts
    amt_reg = round(t_reg * base_rate, 2)
    amt_true_ot = round(t_ot * base_rate, 2) if t_ot > 0 else 0.0
    
    # 2. Differentials
    r_night = round(base_rate * 0.10, 2); amt_night = round(t_night * r_night, 2)
    r_sun = round(base_rate * 0.25, 2); amt_sun = round(t_sun * r_sun, 2)
    amt_hol = round(t_hol * base_rate, 2)
    
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
                 amt_cip = round(amt_reg * factor, 2)
                 r_cip = round(base_rate * factor, 2)

    # 4. FLSA Calculation (Weighted Average)
    amt_flsa = 0.0; r_flsa = 0.0
    if t_ot > 0:
        # Numerator: Base + True OT + Diffs + CIP + OJTI + CIC
        remun = amt_reg + amt_true_ot + amt_night + amt_sun + amt_cip + amt_ojti + amt_cic
        hrs = t_reg + t_ot
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
    if t_reg: rows.append(["Regular", base_rate, t_reg, amt_reg])
    if amt_cip: rows.append(["Controller Incentive Pay", r_cip, t_reg, amt_cip])
    if t_ot:
        rows.append(["FLSA Premium", r_flsa, t_ot, amt_flsa])
        rows.append(["True Overtime", base_rate, t_ot, amt_true_ot])
    if t_night: rows.append(["Night Differential", r_night, t_night, amt_night])
    if t_sun: rows.append(["Sunday Premium", r_sun, t_sun, amt_sun])
    if t_hol: rows.append(["Holiday Worked", base_rate, t_hol, amt_hol])
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
