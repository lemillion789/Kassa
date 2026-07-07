import base64
import json
import logging
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, date
import math
import random
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

# Initialize logging to stderr (stdout is reserved for MCP JSON-RPC protocol)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("finance_mcp_server")

DB_PATH = "finance.db"
mcp = FastMCP("Personal Finance")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Transactions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            merchant TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            date TEXT NOT NULL
        )
    """)
    
    # Budgets table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            category TEXT PRIMARY KEY,
            limit_amount REAL NOT NULL
        )
    """)
    
    # Income table (single row: id=1)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS income (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            monthly_income REAL NOT NULL
        )
    """)
    
    # Savings Goals table (single row: id=1)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS savings_goals (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            monthly_amount REAL NOT NULL
        )
    """)
    
    # Baselines table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS baselines (
            category TEXT PRIMARY KEY,
            avg_spend REAL NOT NULL,
            std_dev REAL NOT NULL
        )
    """)
    
    conn.commit()
    conn.close()

# Initialize DB on import / startup
init_db()

# Define Needs vs Wants for 50/30/20 budgeting strategy
NEEDS_CATEGORIES = {"groceries", "utilities", "rent", "transport", "insurance", "bills"}
WANTS_CATEGORIES = {"dining out", "entertainment", "subscriptions", "shopping", "travel", "leisure"}


@mcp.tool()
def log_transaction(amount: float, merchant: str, category: str, description: str, date_str: str) -> str:
    """Log a transaction into the finance database.

    Args:
        amount: Amount of transaction in SEK.
        merchant: Name of the merchant.
        category: Category of transaction (e.g. Groceries, Rent, Entertainment).
        description: Description of transaction.
        date_str: Date of transaction in YYYY-MM-DD format.

    Returns:
        Confirmation message.
    """
    try:
        # Validate date format
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return f"Error: Date format must be YYYY-MM-DD. Got {date_str}"

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
            (amount, merchant, category, description, date_str)
        )
        conn.commit()
        tx_id = cursor.lastrowid
        return json.dumps({
            "status": "success",
            "message": f"Successfully logged transaction {tx_id}",
            "transaction": {
                "id": tx_id,
                "amount": amount,
                "merchant": merchant,
                "category": category,
                "description": description,
                "date": date_str
            }
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


@mcp.tool()
def monthly_summary(month: str) -> str:
    """Calculate category-wise spend totals for a given month (YYYY-MM).

    Args:
        month: The month to summarize in YYYY-MM format.

    Returns:
        JSON string of summary statistics and category totals.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT category, SUM(amount) as total FROM transactions WHERE date LIKE ? GROUP BY category",
            (f"{month}-%",)
        )
        rows = cursor.fetchall()
        totals = {row["category"]: row["total"] for row in rows}
        overall = sum(totals.values())
        return json.dumps({
            "month": month,
            "category_totals": totals,
            "overall_spend": overall
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


@mcp.tool()
def compute_baselines() -> str:
    """Compute average monthly spend and standard deviation for each category based on historical data.
    
    Excludes the current month to ensure we base limits on complete historical months.
    If no complete prior months exist, it uses all history.

    Returns:
        JSON string containing the computed category baselines.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Get all transactions
        cursor.execute("SELECT amount, category, date FROM transactions")
        txs = cursor.fetchall()
        if not txs:
            return json.dumps({"status": "success", "message": "No transaction history to compute baselines."})
            
        current_month = datetime.today().strftime("%Y-%m")
        
        # Group spends by month and category
        monthly_category_spend = defaultdict(lambda: defaultdict(float))
        all_months = set()
        
        for tx in txs:
            m = tx["date"][:7] # YYYY-MM
            all_months.add(m)
            monthly_category_spend[tx["category"]][m] += tx["amount"]
            
        # Determine complete historical months
        complete_months = sorted(list(all_months - {current_month}))
        if not complete_months:
            # Fallback to all months if no past complete months exist
            complete_months = sorted(list(all_months))
            
        num_months = len(complete_months)
        baselines = {}
        
        # Calculate mean and std dev per category
        for category, spends_by_month in monthly_category_spend.items():
            monthly_amounts = [spends_by_month[m] for m in complete_months]
            # Fill missing months with 0.0 spend
            while len(monthly_amounts) < num_months:
                monthly_amounts.append(0.0)
                
            mean = sum(monthly_amounts) / num_months
            variance = sum((x - mean) ** 2 for x in monthly_amounts) / num_months
            std_dev = math.sqrt(variance)
            
            baselines[category] = {
                "avg_spend": round(mean, 2),
                "std_dev": round(std_dev, 2)
            }
            
            # Persist to database
            cursor.execute(
                "INSERT OR REPLACE INTO baselines (category, avg_spend, std_dev) VALUES (?, ?, ?)",
                (category, mean, std_dev)
            )
            
        conn.commit()
        return json.dumps({
            "status": "success",
            "months_analyzed": complete_months,
            "baselines": baselines
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


@mcp.tool()
def detect_deviations(month: str) -> str:
    """Compare a month's spending against baselines to flag material deviations (>25% above baseline).

    Args:
        month: The month to analyze in YYYY-MM format.

    Returns:
        JSON report of category deviations and alerts.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Load actual spends for target month
        cursor.execute(
            "SELECT category, SUM(amount) as total FROM transactions WHERE date LIKE ? GROUP BY category",
            (f"{month}-%",)
        )
        actuals = {row["category"]: row["total"] for row in cursor.fetchall()}
        
        # Load baselines
        cursor.execute("SELECT category, avg_spend, std_dev FROM baselines")
        baselines = {row["category"]: {"avg_spend": row["avg_spend"], "std_dev": row["std_dev"]} for row in cursor.fetchall()}
        
        deviations = {}
        alerts = []
        
        for category, actual in actuals.items():
            baseline = baselines.get(category)
            if not baseline or baseline["avg_spend"] == 0:
                # No baseline spend: 100% deviation if spent anything
                diff_pct = 100.0
                deviations[category] = {
                    "actual": actual,
                    "baseline": 0.0,
                    "diff_pct": diff_pct,
                    "std_dev_multiple": 0.0,
                    "flagged": True
                }
                alerts.append(f"Unusual spending in {category}: actual {actual} SEK (no baseline).")
                continue
                
            avg = baseline["avg_spend"]
            std = baseline["std_dev"]
            
            diff_pct = ((actual - avg) / avg) * 100.0
            std_dev_multiple = (actual - avg) / std if std > 0 else 0.0
            
            # Flag if spending is 25% or more above baseline
            flagged = diff_pct >= 25.0
            
            deviations[category] = {
                "actual": round(actual, 2),
                "baseline": round(avg, 2),
                "diff_pct": round(diff_pct, 1),
                "std_dev_multiple": round(std_dev_multiple, 2),
                "flagged": flagged
            }
            
            if flagged:
                alerts.append(
                    f"Material deviation in {category}: {round(diff_pct, 1)}% above baseline "
                    f"({round(actual, 2)} SEK vs {round(avg, 2)} SEK)."
                )
                
        return json.dumps({
            "month": month,
            "deviations": deviations,
            "alerts": alerts
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


@mcp.tool()
def set_income(monthly_income: float) -> str:
    """Set the user's monthly income.

    Args:
        monthly_income: Monthly income amount in SEK.

    Returns:
        Confirmation message.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR REPLACE INTO income (id, monthly_income) VALUES (1, ?)", (monthly_income,))
        conn.commit()
        return f"Successfully set monthly income to {monthly_income} SEK."
    except Exception as e:
        return f"Error: {e}"
    finally:
        conn.close()


@mcp.tool()
def get_income() -> str:
    """Retrieve the stored monthly income.

    Returns:
        JSON with the income value or status.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT monthly_income FROM income WHERE id = 1")
        row = cursor.fetchone()
        income = row["monthly_income"] if row else 0.0
        return json.dumps({"income": income})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


@mcp.tool()
def generate_budget_plan(strategy: str) -> str:
    """Propose a monthly budget plan based on actual baselines and income.

    Args:
        strategy: Strategy name: '50/30/20' (needs/wants/savings) or 'baseline_trim' (trim overspent categories).

    Returns:
        JSON proposed budget plan details.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Load income
        cursor.execute("SELECT monthly_income FROM income WHERE id = 1")
        row = cursor.fetchone()
        income = row["monthly_income"] if row else 0.0
        if income <= 0:
            return json.dumps({"status": "error", "message": "Please configure monthly income first via set_income tool."})
            
        # Load baselines
        cursor.execute("SELECT category, avg_spend FROM baselines")
        baselines = {row["category"]: row["avg_spend"] for row in cursor.fetchall()}
        
        proposed_limits = {}
        summary = {}
        
        if strategy == "50/30/20":
            # 50% Needs, 30% Wants, 20% Savings
            target_savings = income * 0.20
            target_needs = income * 0.50
            target_wants = income * 0.30
            
            needs_baselines = {}
            wants_baselines = {}
            
            for cat, val in baselines.items():
                if cat.lower() in NEEDS_CATEGORIES:
                    needs_baselines[cat] = val
                else:
                    wants_baselines[cat] = val
                    
            total_needs_spend = sum(needs_baselines.values())
            total_wants_spend = sum(wants_baselines.values())
            
            # Distribute proportionally to fit the target limits
            for cat, val in needs_baselines.items():
                scale = target_needs / total_needs_spend if total_needs_spend > target_needs else 1.0
                proposed_limits[cat] = round(val * scale, 2)
                
            for cat, val in wants_baselines.items():
                scale = target_wants / total_wants_spend if total_wants_spend > target_wants else 1.0
                proposed_limits[cat] = round(val * scale, 2)
                
            summary = {
                "strategy": "50/30/20",
                "income": income,
                "needs_allocation": target_needs,
                "wants_allocation": target_wants,
                "savings_allocation": target_savings,
                "proposed_savings": target_savings
            }
            
        elif strategy == "baseline_trim":
            # Fit overall baseline spend to income by trimming the highest spent categories
            total_baseline_spend = sum(baselines.values())
            savings_gap = (total_baseline_spend + (income * 0.10)) - income # aim for at least 10% savings
            
            proposed_limits = baselines.copy()
            trimmed_amounts = {}
            
            if savings_gap > 0:
                # Sort categories by highest baseline spend
                sorted_cats = sorted(baselines.items(), key=lambda x: x[1], reverse=True)
                # Trim the top 3 categories by 15% each
                trimmed_sum = 0
                for cat, val in sorted_cats[:3]:
                    trim = val * 0.15
                    proposed_limits[cat] = round(val - trim, 2)
                    trimmed_amounts[cat] = round(trim, 2)
                    trimmed_sum += trim
                implied_savings = income - (total_baseline_spend - trimmed_sum)
            else:
                implied_savings = income - total_baseline_spend
                
            summary = {
                "strategy": "baseline_trim",
                "income": income,
                "total_baseline_spend": total_baseline_spend,
                "trimmed_categories": trimmed_amounts,
                "proposed_savings": round(max(0.0, implied_savings), 2)
            }
            
        else:
            return json.dumps({"status": "error", "message": f"Unsupported strategy '{strategy}'. Use '50/30/20' or 'baseline_trim'."})
            
        # Persist proposed limits to budgets table
        for cat, val in proposed_limits.items():
            cursor.execute("INSERT OR REPLACE INTO budgets (category, limit_amount) VALUES (?, ?)", (cat, val))
        conn.commit()
        
        return json.dumps({
            "summary": summary,
            "proposed_limits": proposed_limits
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


@mcp.tool()
def set_savings_goal(monthly_amount: float) -> str:
    """Set the user's monthly savings goal.

    Args:
        monthly_amount: Savings goal in SEK.

    Returns:
        Confirmation message.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR REPLACE INTO savings_goals (id, monthly_amount) VALUES (1, ?)", (monthly_amount,))
        conn.commit()
        return f"Successfully set monthly savings goal to {monthly_amount} SEK."
    except Exception as e:
        return f"Error: {e}"
    finally:
        conn.close()


@mcp.tool()
def savings_progress(month: str) -> str:
    """Track progress towards the savings goal for a given month.

    Args:
        month: The month to check in YYYY-MM format.

    Returns:
        JSON savings analysis.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Load income
        cursor.execute("SELECT monthly_income FROM income WHERE id = 1")
        row = cursor.fetchone()
        income = row["monthly_income"] if row else 0.0
        
        # Load savings goal
        cursor.execute("SELECT monthly_amount FROM savings_goals WHERE id = 1")
        row = cursor.fetchone()
        goal = row["monthly_amount"] if row else 0.0
        
        # Calculate actual spend for the month
        cursor.execute(
            "SELECT SUM(amount) as total FROM transactions WHERE date LIKE ?",
            (f"{month}-%",)
        )
        row = cursor.fetchone()
        actual_spend = row["total"] if row and row["total"] else 0.0
        
        actual_saved = income - actual_spend
        savings_gap = goal - actual_saved
        
        advice = []
        if savings_gap > 0:
            # Load top categories with highest spend for this month
            cursor.execute(
                "SELECT category, SUM(amount) as total FROM transactions WHERE date LIKE ? GROUP BY category ORDER BY total DESC",
                (f"{month}-%",)
            )
            top_spends = cursor.fetchall()
            advice.append(f"You missed your savings goal by {round(savings_gap, 2)} SEK.")
            if top_spends:
                advice.append("To close the gap, consider trimming spending in your largest categories:")
                for row in top_spends[:2]:
                    advice.append(f"- {row['category']}: current spend {row['total']} SEK.")
        else:
            advice.append("Great job! You exceeded your monthly savings goal!")
            
        return json.dumps({
            "month": month,
            "income": income,
            "savings_goal": goal,
            "actual_saved": round(actual_saved, 2),
            "savings_gap": round(max(0.0, savings_gap), 2),
            "advice": advice
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


@mcp.tool()
def detect_subscriptions() -> str:
    """Identify repeating transactions occurring monthly with identical merchant and similar amount.
    
    Also flags phantom subscriptions (not billed in the last 45 days).

    Returns:
        JSON list of identified subscriptions.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Load all transactions sorted by merchant and date
        cursor.execute("SELECT amount, merchant, category, date FROM transactions ORDER BY merchant, date")
        txs = cursor.fetchall()
        
        merchant_txs = defaultdict(list)
        for tx in txs:
            merchant_txs[tx["merchant"]].append(tx)
            
        subscriptions = []
        today = datetime.today().date()
        
        for merchant, history in merchant_txs.items():
            if len(history) < 2:
                continue
                
            intervals = []
            amounts = []
            
            for i in range(len(history) - 1):
                d1 = datetime.strptime(history[i]["date"], "%Y-%m-%d").date()
                d2 = datetime.strptime(history[i+1]["date"], "%Y-%m-%d").date()
                interval = (d2 - d1).days
                intervals.append(interval)
                amounts.append(history[i]["amount"])
            amounts.append(history[-1]["amount"])
            
            # Calculate metrics
            avg_interval = sum(intervals) / len(intervals)
            avg_amount = sum(amounts) / len(amounts)
            
            # Check if cadence matches a monthly cycle (27 to 33 days)
            # and amount is stable (variance is low)
            is_monthly = all(25 <= inv <= 35 for inv in intervals)
            is_amount_stable = all(abs(amt - avg_amount) < (avg_amount * 0.10) for amt in amounts) # within 10%
            
            if is_monthly and is_amount_stable:
                last_billed_date = datetime.strptime(history[-1]["date"], "%Y-%m-%d").date()
                days_since_billing = (today - last_billed_date).days
                
                # Flag as phantom if not billed in last 45 days
                is_phantom = days_since_billing > 45
                
                subscriptions.append({
                    "merchant": merchant,
                    "category": history[0]["category"],
                    "avg_amount": round(avg_amount, 2),
                    "last_billed": str(last_billed_date),
                    "days_since_billed": days_since_billing,
                    "is_phantom": is_phantom
                })
                
        return json.dumps({
            "status": "success",
            "detected_subscriptions": subscriptions
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


@mcp.tool()
def project_month_end(month: str) -> str:
    """Project category and total spend for the end of the month based on current run rate.

    Args:
        month: The month to project in YYYY-MM format.

    Returns:
        JSON containing projections compared to plan.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Load category limits (budgets)
        cursor.execute("SELECT category, limit_amount FROM budgets")
        budgets = {row["category"]: row["limit_amount"] for row in cursor.fetchall()}
        
        # Load transactions for the month
        cursor.execute(
            "SELECT category, SUM(amount) as total FROM transactions WHERE date LIKE ? GROUP BY category",
            (f"{month}-%",)
        )
        actuals = {row["category"]: row["total"] for row in cursor.fetchall()}
        
        # Calculate extrapolation factor based on day of month
        today = datetime.today()
        current_year_month = today.strftime("%Y-%m")
        
        if month == current_year_month:
            # We scale current month based on the day
            current_day = today.day
            # Find total days in current month
            import calendar
            total_days = calendar.monthrange(today.year, today.month)[1]
            scale_factor = total_days / current_day
        else:
            # Past month is complete
            scale_factor = 1.0
            
        projections = {}
        for cat, actual in actuals.items():
            proj = actual * scale_factor
            limit = budgets.get(cat, 0.0)
            over_budget = proj > limit if limit > 0 else False
            
            projections[cat] = {
                "actual_so_far": round(actual, 2),
                "projected_month_end": round(proj, 2),
                "limit": limit,
                "over_budget_projected": over_budget
            }
            
        overall_actual = sum(actuals.values())
        overall_projected = overall_actual * scale_factor
        overall_limit = sum(budgets.values())
        
        return json.dumps({
            "month": month,
            "scale_factor": round(scale_factor, 3),
            "projections": projections,
            "overall_summary": {
                "actual_so_far": round(overall_actual, 2),
                "projected_month_end": round(overall_projected, 2),
                "limit": round(overall_limit, 2),
                "over_budget_projected": overall_projected > overall_limit if overall_limit > 0 else False
            }
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


@mcp.tool()
def collect_insights(month: str) -> str:
    """Gather top deviations, active/phantom subscriptions, over-budget categories, and savings gap.

    Args:
        month: The month to gather insights for in YYYY-MM format.

    Returns:
        JSON summary package of insights.
    """
    try:
        # 1. Load deviations
        dev_res = json.loads(detect_deviations(month))
        deviations = [
            {"category": cat, "diff_pct": val["diff_pct"]}
            for cat, val in dev_res.get("deviations", {}).items()
            if val.get("flagged")
        ]
        
        # 2. Load subscriptions
        sub_res = json.loads(detect_subscriptions())
        subscriptions = sub_res.get("detected_subscriptions", [])
        active_subs = [s for s in subscriptions if not s["is_phantom"]]
        phantom_subs = [s for s in subscriptions if s["is_phantom"]]
        
        # 3. Load budget plan projections
        proj_res = json.loads(project_month_end(month))
        over_budget_categories = [
            {"category": cat, "projected": val["projected_month_end"], "limit": val["limit"]}
            for cat, val in proj_res.get("projections", {}).items()
            if val.get("over_budget_projected")
        ]
        
        # 4. Load savings gap
        sav_res = json.loads(savings_progress(month))
        savings_gap = sav_res.get("savings_gap", 0.0)
        
        return json.dumps({
            "month": month,
            "top_deviations": deviations,
            "active_subscriptions": active_subs,
            "phantom_subscriptions": phantom_subs,
            "over_budget_categories": over_budget_categories,
            "savings_gap": savings_gap
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
@mcp.tool()
def seed_synthetic_data(months: int = 8, seed: int = 42) -> str:
    """Wipes the database and populates it with realistic synthetic data for a single person in Sweden.
    
    Includes an 18,000 SEK income, recurring subscriptions, variable spends, and a deliberate overspend month.

    Args:
        months: Number of months of transaction history to generate.
        seed: Random seed for reproducibility.

    Returns:
        JSON string describing the seed success and generated counts.
    """
    import random
    random.seed(seed)
    
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Wipe database tables
        cursor.execute("DELETE FROM transactions")
        cursor.execute("DELETE FROM budgets")
        cursor.execute("DELETE FROM income")
        cursor.execute("DELETE FROM savings_goals")
        cursor.execute("DELETE FROM baselines")
        
        # 1. Income (18,000 SEK)
        cursor.execute("INSERT INTO income (id, monthly_income) VALUES (1, 18000.0)")
        
        # 2. Savings Goal (3000 SEK)
        cursor.execute("INSERT INTO savings_goals (id, monthly_amount) VALUES (1, 3000.0)")
        
        # Determine the months range
        today = datetime.today().date()
        
        # Collect YYYY-MM strings for the target months
        target_months = []
        cy, cm = today.year, today.month
        for i in range(months):
            m_idx = cm - i
            y_offset = (m_idx - 1) // 12 if m_idx <= 0 else 0
            month_val = (m_idx - 1) % 12 + 1
            target_months.append(f"{cy + y_offset}-{month_val:02d}")
            
        # Sort months chronologically
        target_months.sort()
        
        # Deliberate overspend month: make it the second most recent complete month
        overspend_month = target_months[-2] if len(target_months) >= 2 else target_months[0]
        
        tx_count = 0
        
        for m_str in target_months:
            # --- Rent (Fixed: 6500 SEK) ---
            cursor.execute(
                "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
                (6500.0, "Svea Fastigheter", "Rent", "Monthly apartment rent", f"{m_str}-01")
            )
            tx_count += 1
            
            # --- Subscriptions (Fixed recurring monthly) ---
            # Gym: 350 SEK
            cursor.execute(
                "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
                (350.0, "Friskis&Svettis", "Subscriptions", "Monthly gym membership", f"{m_str}-05")
            )
            # Music: 119 SEK
            cursor.execute(
                "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
                (119.0, "Spotify", "Subscriptions", "Premium subscription", f"{m_str}-10")
            )
            # Streaming: 149 SEK
            cursor.execute(
                "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
                (149.0, "Netflix", "Subscriptions", "Family plan streaming", f"{m_str}-15")
            )
            tx_count += 3
            
            # --- Transport (Fixed: 970 SEK SL ticket) ---
            cursor.execute(
                "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
                (970.0, "SL Stockholm", "Transport", "Monthly transit card", f"{m_str}-02")
            )
            tx_count += 1
            
            # --- Groceries (Varies: 2000 to 3000 SEK, spent across 4 grocery trips) ---
            num_trips = 4
            for trip in range(1, num_trips + 1):
                day = trip * 7 - random.randint(0, 2)
                amount = round(random.uniform(500.0, 750.0), 2)
                merchant = random.choice(["ICA Kvantum", "Coop", "Willys"])
                cursor.execute(
                    "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
                    (amount, merchant, "Groceries", f"Weekly groceries trip {trip}", f"{m_str}-{day:02d}")
                )
                tx_count += 1
                
            # --- Eating Out (Normal: 400-800 SEK; Overspend month: 3000 SEK) ---
            if m_str == overspend_month:
                for trip in range(6):
                    day = random.randint(1, 28)
                    amount = round(random.uniform(450.0, 550.0), 2) # averages ~3000 SEK
                    merchant = random.choice(["Espresso House", "Restaurang Gyllene Freden", "Max Hamburgare", "Sushibar"])
                    cursor.execute(
                        "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
                        (amount, merchant, "Eating Out", f"Overspend dinner outing {trip+1}", f"{m_str}-{day:02d}")
                    )
                    tx_count += 1
            else:
                for trip in range(random.randint(2, 3)):
                    day = random.randint(1, 28)
                    amount = round(random.uniform(150.0, 250.0), 2)
                    merchant = random.choice(["Espresso House", "Max Hamburgare", "Pizzeria Stockholm"])
                    cursor.execute(
                        "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
                        (amount, merchant, "Eating Out", f"Normal dining outing", f"{m_str}-{day:02d}")
                    )
                    tx_count += 1
                    
            # --- Shopping (Normal: 300-800 SEK; Overspend month: 4700 SEK) ---
            if m_str == overspend_month:
                cursor.execute(
                    "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
                    (4200.0, "Elgiganten", "Shopping", "New electronics purchase", f"{m_str}-18")
                )
                cursor.execute(
                    "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
                    (500.0, "H&M", "Shopping", "Clothes shopping", f"{m_str}-22")
                )
                tx_count += 2
            else:
                if random.random() > 0.5:
                    day = random.randint(1, 28)
                    amount = round(random.uniform(200.0, 600.0), 2)
                    merchant = random.choice(["H&M", "IKEA", "Clas Ohlson"])
                    cursor.execute(
                        "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
                        (amount, merchant, "Shopping", "Routine shopping item", f"{m_str}-{day:02d}")
                    )
                    tx_count += 1
                    
            # --- Health & Misc ---
            if random.random() > 0.7:
                day = random.randint(1, 28)
                amount = round(random.uniform(150.0, 300.0), 2)
                cursor.execute(
                    "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
                    (amount, "Apoteket", "Health", "Prescriptions / medicine", f"{m_str}-{day:02d}")
                )
                tx_count += 1
                
            day = random.randint(1, 28)
            amount = round(random.uniform(100.0, 250.0), 2)
            cursor.execute(
                "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
                (amount, "Pressbyran", "Misc", "Convenience items", f"{m_str}-{day:02d}")
            )
            tx_count += 1
            
        conn.commit()
        return json.dumps({
            "status": "success",
            "message": f"Successfully cleared and seeded finance database with {tx_count} transactions across {months} months.",
            "income": 18000.0,
            "savings_goal": 3000.0,
            "overspend_month": overspend_month,
            "months_seeded": target_months
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
