import pandas as pd

def get_css():
    return """
    <style>
        .audit-error { color: red !important; font-weight: bold; text-decoration: underline wavy red; cursor: help; }
        .comparison-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .comp-col h3 { text-align: center; padding: 10px; color: white; border-radius: 5px; }
        .comp-expected { border-top: 5px solid #2e86c1; }
        .comp-actual { border-top: 5px solid #27ae60; }
        .stub-wrapper { background: white; padding: 15px; border: 1px solid #ddd; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
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

    html = '<div class="stub-wrapper"><table class="table" style="width:100%">'
    
    # Header
    html += f'''
    <tr><td colspan="4" style="text-align:center"><b>{stub['agency']}</b><br>Period Ending: {stub['period_ending']}</td></tr>
    <tr>
        <td>Gross: {val(stub['gross_pay'], 'gross_pay')}</td>
        <td>Net: {val(stub['net_pay'], 'net_pay')}</td>
    </tr>
    '''
    
    # Earnings
    html += '<tr><td colspan="4" style="background:#eee"><b>Earnings</b></td></tr>'
    for _, r in data['earnings'].iterrows():
        html += f"<tr><td>{r['type']}</td><td>Rate: {val(r['rate'], money=False)}</td><td>Hrs: {val(r['hours_current'], money=False)}</td><td>Amt: {val(r['amount_current'])}</td></tr>"

    # Deductions (Summarized)
    if not data['deductions'].empty:
        html += '<tr><td colspan="4" style="background:#eee"><b>Deductions</b></td></tr>'
        html += f"<tr><td colspan='4'>Total Deductions: {val(data['deductions']['amount_current'].sum())}</td></tr>"

    html += '</table></div>'
    return html
