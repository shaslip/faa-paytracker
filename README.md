# FAA PayTracker & Audit System

A local Python-based dashboard for government employees to audit paystubs, detect payroll anomalies, and track "Shadow" payments during government shutdowns.

## Features

* **Automated Auditing:** Instantly flags if Leave balances don't match the math (Start + Earned - Used = End).
* **Anomaly Detection:** Alerts you if new deduction codes appear or tax rates shift unexpectedly.
* **Visual Replica:** Recreates the Employee Express paystub exactly but highlights errors in **Red** with explanatory tooltips.
* **Shutdown Ready:** Includes a "Shadow Ledger" to project missed paychecks during a shutdown and reconcile them against the eventual "Lump Sum" payout.
* **Trends:** Visualizes Gross vs. Net pay over time.

## Prerequisites

You need Python installed. This project relies on the following libraries:

```bash
pip install streamlit pandas beautifulsoup4
````

## Folder Structure

Ensure your project folder looks like this:

```
PayTracker/
├── PayStubs/          # <--- Drop your HTML files here
├── ingest.py          # The database builder script
├── dashboard.py       # The visualization app
├── style.css          # Styling for the paystub replica
└── payroll_audit.db   # (Created automatically)
```

## How to Use

### 1\. Prepare Your Data

1.  Log in to Employee Express.
2.  Open your Earnings and Leave Statement.
3.  Save the page as **HTML Only** (or "Webpage, Complete").
4.  **Crucial:** Rename the file to the **Pay Period Ending** date using this exact format:
      * `YYYY-MM-DD.html`
      * Example: `2025-11-29.html`
5.  Move these files into the `PayStubs/` folder.

### 2\. Ingest the Data

Run the ingestion script to parse the HTML files and build the database.

```bash
python3 ingest.py
```

*Note: You can also trigger this button from the "Ingestion" tab inside the dashboard.*

### 3\. Launch the Dashboard

Start the visual interface:

```bash
streamlit run dashboard.py
```

Your browser will open to `http://localhost:8501`.

## The Workflow

### The "Deep Dive Audit" Tab

  * Select a pay period from the dropdown.
  * The system runs math checks on Leave, Gross Pay, and Net Pay.
  * **Red Text** indicates an error. Hover over the number to see the math discrepancy.
  * *Note:* If the Remarks section mentions "LEAVE ADJUSTMENT", errors may be flagged as warnings instead.

### The "Projection" Tab (Shutdown Mode)

  * If the government shuts down, use this tab to input your hours (Regular, OT, Night Diff).
  * Click **"Save to Shadow Ledger"**.
  * This creates a placeholder record in your history so you can track what you are owed.

## Troubleshooting

  * **HTML Rendering looks like code:** Ensure you are using the updated `dashboard.py` where indentation was stripped from the HTML strings.
  * **Tabs look tiny:** Ensure you are using the "Clean" version of `style.css` (not the full raw CSS from Employee Express).
  * **Database Errors:** If the schema changes (e.g., adding new columns), delete `payroll_audit.db` and run `python3 ingest.py` to rebuild it fresh.
