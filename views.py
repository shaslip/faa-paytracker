import pandas as pd
import os

def get_css():
    """Reads the external style.css file."""
    css_file = 'style.css'
    if os.path.exists(css_file):
        with open(css_file) as f:
            return f'<style>{f.read()}</style>'
    return "<style></style>"

def render_paystub_html(data, flags=None, mode="actual"):
    if flags is None: flags = {}
    stub = data['stub']

    def val(v, fid=None, money=True):
        if pd.isna(v): return "0.00"
        txt = f"{v:,.2f}" if money and isinstance(v, (int,float)) else str(v)
        if fid and fid in flags: return f'<span class="audit-error" title="{flags[fid]}">{txt}</span>'
        return txt

    parts = []
    # Compact HTML to avoid Markdown parsing issues
    parts.append('<div class="stub-wrapper"><div id="elsInfoTable">')
    parts.append('<table class="table els-table" cellpadding="0" cellspacing="0"><tbody>')
    
    # --- HEADER SECTION ---
    # Col-6: Agency | Col-3: Period Ending | Col-3: Net Pay
    parts.append(f'''
    <tr>
        <td colspan="6" rowspan="2" class="col-6">
            <span class="text-align-center cell-title-lg2">{stub['agency']}</span><br>
            <span class="text-align-center cell-title-lg2">Earnings and Leave Statement</span>
        </td>
        <td colspan="3" class="col-3">
            <span class="cell-title">For Pay Period Ending</span><span>{stub['period_ending']}</span>
        </td>
        <td colspan="3" class="col-3 no-margin-padding">
            <span class="cell-title blue">Net Pay</span><span class="cell">$ {val(stub['net_pay'], 'net_pay')}</span>
        </td>
    </tr>
    <tr>
        <td colspan="3"><span class="cell-title">Pay Date</span><span>{stub['pay_date']}</span></td>
        <td colspan="3"></td>
    </tr>
    ''')

    # --- SUMMARY TABLE (Nested) ---
    parts.append(f'''
    <tr>
        <td colspan="5" class="no-margin-padding">
            <table class="table no-margin-padding no-border">
                <tr><th class="col-6 blue no-border">Your Pay Consists of</th><th class="col-3 blue no-border text-align-right">Current</th></tr>
                <tr><td>Gross Pay</td><td class="text-align-right">{val(stub['gross_pay'], 'gross_pay')}</td></tr>
                <tr><td>Total Deductions</td><td class="text-align-right">{val(stub['total_deductions'])}</td></tr>
                <tr><td>Net Pay</td><td class="text-align-right">{val(stub['net_pay'])}</td></tr>
            </table>
        </td>
        <td colspan="7"></td>
    </tr>
    ''')

    # --- EARNINGS SECTION ---
    parts.append('<tr><td colspan="12" class="blue"><span class="text-align-center cell-title-lg">Earnings</span></td></tr>')
    parts.append('<tr><td colspan="12"><table class="table no-border no-margin-padding">')
    parts.append('<tr><th class="col-5">Type</th><th class="col-1 text-align-right">Rate</th><th class="col-1 text-align-right">Hours</th><th class="col-1 text-align-right">Current</th><th class="col-1 text-align-right">YTD</th></tr>')
    
    if not data['earnings'].empty:
        for _, r in data['earnings'].iterrows():
            parts.append(f'''
            <tr>
                <td>{r['type']}</td>
                <td class="text-align-right">{val(r['rate'], money=False)}</td>
                <td class="text-align-right">{val(r['hours_current'], money=False)}</td>
                <td class="text-align-right">{val(r['amount_current'])}</td>
                <td class="text-align-right">{val(r['amount_ytd'])}</td>
            </tr>
            ''')
    parts.append('</table></td></tr>')

    # --- DEDUCTIONS SECTION ---
    if not data['deductions'].empty:
        parts.append('<tr><td colspan="12" class="blue"><span class="text-align-center cell-title-lg">Deductions</span></td></tr>')
        parts.append('<tr><td colspan="12"><table class="table no-border no-margin-padding">')
        parts.append('<tr><th class="col-4">Type</th><th class="col-2 text-align-right">Current</th><th class="col-2 text-align-right">YTD</th></tr>')
        
        for _, r in data['deductions'].iterrows():
             parts.append(f'''
             <tr>
                <td>{r['type']}</td>
                <td class="text-align-right">{val(r['amount_current'])}</td>
                <td class="text-align-right">{val(r['amount_ytd'])}</td>
             </tr>
             ''')
        parts.append('</table></td></tr>')

    # --- LEAVE SECTION ---
    if not data['leave'].empty:
        parts.append('<tr><td colspan="12" class="blue"><span class="text-align-center cell-title-lg">Leave</span></td></tr>')
        parts.append('<tr><td colspan="12"><table class="table no-border no-margin-padding">')
        parts.append('<tr><th class="col-2">Type</th><th class="col-1 text-align-right">Start Bal</th><th class="col-1 text-align-right">Earned</th><th class="col-1 text-align-right">Used</th><th class="col-1 text-align-right">End Bal</th></tr>')
        
        for _, r in data['leave'].iterrows():
            fid = f"leave_{r['type']}_end"
            parts.append(f'''
            <tr>
                <td>{r['type']}</td>
                <td class="text-align-right">{val(r['balance_start'], money=False)}</td>
                <td class="text-align-right">{val(r['earned_current'], money=False)}</td>
                <td class="text-align-right">{val(r['used_current'], money=False)}</td>
                <td class="text-align-right">{val(r['balance_end'], fid, money=False)}</td>
            </tr>
            ''')
        parts.append('</table></td></tr>')

    # --- REMARKS ---
    if stub.get('remarks'):
        rem = stub['remarks'].replace('\n', '<br>')
        parts.append(f'<tr><td colspan="12" class="blue"><span class="text-align-center cell-title-lg">Remarks</span></td></tr><tr><td colspan="12" style="padding:10px"><span style="font-family:monospace">{rem}</span></td></tr>')

    parts.append('</tbody></table></div></div>')
    return "".join(parts)
