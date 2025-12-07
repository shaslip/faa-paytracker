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
