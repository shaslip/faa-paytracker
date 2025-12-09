import pandas as pd

def get_css():
    return """
    <style>
        .audit-error { color: red !important; font-weight: bold !important; text-decoration: underline wavy red !important; cursor: help !important; }
        .comparison-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .comp-col h3 { text-align: center; padding: 10px; color: white; border-radius: 5px; }
        .comp-expected { border-top: 5px solid #2e86c1; }
        .comp-actual { border-top: 5px solid #27ae60; }
        .stub-wrapper { max-width: 1000px; margin: 0 auto; background: white; padding: 20px; border: 1px solid #ddd; }
    </style>
    """

def render_paystub_html(data, flags=None, mode="actual"):
    if flags is None: flags = {}
    stub = data['stub']

    def val(v, fid=None, money=True):
        if pd.isna(v): return "0.00"
        txt = f"{v:,.2f}" if money and isinstance(v, (int,float)) else str(v)
        if fid and fid in flags: return f'<span class="audit-error" title="{flags[fid]}">{txt}</span>'
        return txt

    parts = []
    parts.append('<div class="stub-wrapper"><div id="elsInfoTable">')
    parts.append('<table class="table els-table" cellpadding="0" cellspacing="0" style="width:100%"><tbody>')
    
    # Header (Your exact structure)
    parts.append(f'''
    <tr>
        <td colspan="6" rowspan="2" class="col-6">
            <span class="text-align-center cell-title-lg2">{stub['agency']}</span><br>
            <span class="text-align-center cell-title-lg2">Earnings and Leave Statement</span>
        </td>
        <td colspan="3" class="col-3">
            <span class="cell-title">For Pay Period Ending</span><br><span>{stub['period_ending']}</span>
        </td>
        <td colspan="3" class="col-3 no-margin-padding">
            <span class="cell-title blue">Net Pay</span><br><span class="cell">$ {val(stub['net_pay'], 'net_pay')}</span>
        </td>
    </tr>
    <tr><td colspan="6"></td></tr>
    ''')

    # Earnings
    parts.append('<tr><td colspan="12" class="blue"><span class="cell-title-lg">Earnings</span></td></tr>')
    parts.append('<tr><th colspan="4">Type</th><th colspan="2" class="text-align-right">Rate</th><th colspan="2" class="text-align-right">Hours</th><th colspan="4" class="text-align-right">Amount</th></tr>')
    
    if not data['earnings'].empty:
        for _, r in data['earnings'].iterrows():
            parts.append(f'''
            <tr>
                <td colspan="4">{r['type']}</td>
                <td colspan="2" class="text-align-right">{val(r['rate'], money=False)}</td>
                <td colspan="2" class="text-align-right">{val(r['hours_current'], money=False)}</td>
                <td colspan="4" class="text-align-right">{val(r['amount_current'])}</td>
            </tr>
            ''')

    # Deductions
    if not data['deductions'].empty:
        parts.append('<tr><td colspan="12" class="blue"><span class="cell-title-lg">Deductions</span></td></tr>')
        for _, r in data['deductions'].iterrows():
             parts.append(f"<tr><td colspan='8'>{r['type']}</td><td colspan='4' class='text-align-right'>{val(r['amount_current'])}</td></tr>")
             
    # Leave
    if not data['leave'].empty:
        parts.append('<tr><td colspan="12" class="blue"><span class="cell-title-lg">Leave</span></td></tr>')
        parts.append('<tr><th colspan="4">Type</th><th colspan="2" class="text-align-right">Start</th><th colspan="2" class="text-align-right">Earn</th><th colspan="2" class="text-align-right">Used</th><th colspan="2" class="text-align-right">End</th></tr>')
        for _, r in data['leave'].iterrows():
            fid = f"leave_{r['type']}_end"
            parts.append(f'''
            <tr>
                <td colspan="4">{r['type']}</td>
                <td colspan="2" class="text-align-right">{val(r['balance_start'], money=False)}</td>
                <td colspan="2" class="text-align-right">{val(r['earned_current'], money=False)}</td>
                <td colspan="2" class="text-align-right">{val(r['used_current'], money=False)}</td>
                <td colspan="2" class="text-align-right">{val(r['balance_end'], fid, money=False)}</td>
            </tr>
            ''')

    parts.append('</tbody></table></div></div>')
    return "".join(parts)
