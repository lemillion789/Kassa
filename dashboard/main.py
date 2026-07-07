import os
import json
import sqlite3
import datetime
from collections import defaultdict
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# Load local environment variables (including GEMINI_API_KEY)
from dotenv import load_dotenv
load_dotenv()

# Import the local chat agent from our package
from finance_agent.agent import chat_agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.genai import types

app = FastAPI(title="Finance Manager Dashboard")

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "finance.db")
PENDING_FILE = os.path.join(BASE_DIR, "pending_reviews.json")

# In-process Chat Runner setup
session_service = InMemorySessionService()
chat_runner = Runner(agent=chat_agent, session_service=session_service, app_name="dashboard_chat", auto_create_session=True)

class ActionRequest(BaseModel):
    action: str  # "approve" or "flag"
    category: str | None = None  # Confirmed category if approved

class ChatRequest(BaseModel):
    message: str
    session_id: str = "dashboard-chat-session"

def get_db_connection():
    if not os.path.exists(DB_PATH):
        raise HTTPException(status_code=500, detail="Database file finance.db not found. Please run make seed first.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_pending_reviews():
    if not os.path.exists(PENDING_FILE):
        return []
    try:
        with open(PENDING_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def save_pending_reviews(reviews):
    try:
        with open(PENDING_FILE, "w") as f:
            json.dump(reviews, f, indent=2)
    except Exception:
        pass

@app.get("/", response_class=HTMLResponse)
def get_dashboard():
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")
    if not os.path.exists(template_path):
        raise HTTPException(status_code=404, detail="Dashboard template not found.")
    with open(template_path, "r") as f:
        return f.read()

@app.get("/api/pending")
def get_pending():
    return get_pending_reviews()

@app.post("/api/action/{id}")
def handle_pending_action(id: str, req: ActionRequest):
    reviews = get_pending_reviews()
    matching_items = [r for r in reviews if r.get("id") == id]
    if not matching_items:
        raise HTTPException(status_code=404, detail="Pending review item not found.")
        
    item = matching_items[0]
    
    # Remove from pending list
    reviews = [r for r in reviews if r.get("id") != id]
    save_pending_reviews(reviews)
    
    # Determine target category
    if req.action.lower() == "flag":
        target_category = "Flagged"
    else:
        target_category = req.category or item.get("category") or "Uncategorized"
        
    # Write to finance.db
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO transactions (amount, merchant, category, description, date) VALUES (?, ?, ?, ?, ?)",
            (
                float(item.get("amount", 0.0)),
                str(item.get("merchant", "Unknown")),
                str(target_category),
                str(item.get("description", "")),
                str(item.get("date", datetime.date.today().strftime("%Y-%m-%d")))
            )
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database insert error: {e}")
    finally:
        conn.close()
        
    return {"status": "success", "message": f"Transaction recorded as {target_category}."}

@app.get("/api/insights")
def get_insights(month: str | None = None):
    if not month:
        # Default to last seeded/current month (e.g. 2026-06)
        month = "2026-06"
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # 1. Fetch Income
        cursor.execute("SELECT monthly_income FROM income LIMIT 1")
        row = cursor.fetchone()
        income = row["monthly_income"] if row else 18000.0
        
        # 2. Fetch savings goal
        cursor.execute("SELECT monthly_amount FROM savings_goals LIMIT 1")
        row = cursor.fetchone()
        savings_goal = row["monthly_amount"] if row else 3000.0
        
        # 3. Load actual spends for month
        cursor.execute(
            "SELECT category, SUM(amount) as total FROM transactions WHERE date LIKE ? GROUP BY category",
            (f"{month}-%",)
        )
        actuals = {row["category"]: row["total"] for row in cursor.fetchall()}
        
        # 4. Compute Dynamic Baselines
        # Excludes the month we are analyzing to get the true historical averages
        cursor.execute("SELECT amount, category, date FROM transactions")
        txs = cursor.fetchall()
        
        monthly_category_spend = defaultdict(lambda: defaultdict(float))
        all_months = set()
        for tx in txs:
            m = tx["date"][:7]  # YYYY-MM
            all_months.add(m)
            monthly_category_spend[tx["category"]][m] += tx["amount"]
            
        # Determine complete historical months (excluding the month we are analyzing)
        complete_months = sorted(list(all_months - {month}))
        if not complete_months:
            # Fallback to all months if no other complete months exist
            complete_months = sorted(list(all_months))
            
        num_months = len(complete_months)
        dynamic_baselines = {}
        for cat, spends_by_month in monthly_category_spend.items():
            monthly_amounts = [spends_by_month[m] for m in complete_months]
            mean = sum(monthly_amounts) / num_months if num_months > 0 else 0.0
            dynamic_baselines[cat] = mean
        
        # 5. Load category budgets
        cursor.execute("SELECT category, limit_amount FROM budgets")
        budgets = {row["category"]: row["limit_amount"] for row in cursor.fetchall()}
        
        # Projections and Deviations calculation
        overall_actual = sum(actuals.values())
        
        # Determine extrapolation factor
        today = datetime.datetime.today()
        current_year_month = today.strftime("%Y-%m")
        too_early_to_project = False
        
        if month == current_year_month:
            current_day = today.day
            if current_day < 5:
                # Too early to project - scale factor is 1.0 (current total only)
                scale_factor = 1.0
                too_early_to_project = True
            else:
                import calendar
                total_days = calendar.monthrange(today.year, today.month)[1]
                scale_factor = total_days / current_day
        else:
            scale_factor = 1.0
            
        overall_projected = overall_actual * scale_factor
        
        category_bars = []
        for cat in set(list(actuals.keys()) + list(dynamic_baselines.keys())):
            actual = actuals.get(cat, 0.0)
            baseline = dynamic_baselines.get(cat, 0.0)
            limit = budgets.get(cat, 0.0)
            
            category_bars.append({
                "category": cat,
                "actual": round(actual, 2),
                "baseline": round(baseline, 2),
                "limit": round(limit, 2),
                "ratio": round(actual / baseline, 2) if baseline > 0 else 1.0
            })
            
        # Top deviations for section C
        deviations = []
        for cat, act in actuals.items():
            baseline_spend = dynamic_baselines.get(cat, 0.0)
            if baseline_spend > 0:
                diff = act - baseline_spend
                diff_pct = (diff / baseline_spend) * 100.0
                if diff_pct > 25.0:
                    deviations.append({
                        "category": cat,
                        "actual": act,
                        "baseline": baseline_spend,
                        "diff": diff,
                        "diff_pct": diff_pct
                    })
        # Sort by biggest absolute SEK diff descending
        deviations.sort(key=lambda x: x["diff"], reverse=True)
        
        # Generate Top Savings Actions
        actions = []
        
        # Action A: Deviations (Wants)
        for dev in deviations[:2]:
            actions.append({
                "action": f"Trim spending on {dev['category']}",
                "impact": f"-{round(dev['diff'], 0)} SEK/mo",
                "explanation": f"Spending is {round(dev['diff_pct'], 0)}% over your average baseline of {round(dev['baseline'], 0)} SEK."
            })
            
        # Action B: Savings Gap (Sane and capped calculation)
        remaining = income - overall_projected
        savings_gap = savings_goal - remaining
        if savings_gap > 0:
            # Cap the proposed savings/cut impact to what they actually spend
            impact_amt = min(savings_gap, overall_projected)
            # Also ensure we don't propose saving more than income allows
            if impact_amt > 0 and remaining < income:
                actions.append({
                    "action": "Close your monthly savings gap",
                    "impact": f"-{round(impact_amt, 0)} SEK/mo",
                    "explanation": f"You are currently projected to save {round(remaining, 0)} SEK, which is short of your {round(savings_goal, 0)} SEK goal."
                })
            
        # Ensure we always return exactly 3 actions (with fallbacks if database is empty/fresh)
        while len(actions) < 3:
            if len(actions) == 0:
                actions.append({
                    "action": "Review eating out baseline",
                    "impact": "-400 SEK/mo",
                    "explanation": "Groceries and eating out are your primary variable spend categories."
                })
            elif len(actions) == 1:
                actions.append({
                    "action": "Audit active streaming subscriptions",
                    "impact": "-189 SEK/mo",
                    "explanation": "Identify and cancel gym or streaming memberships that are currently unused."
                })
            else:
                actions.append({
                    "action": "Propose baseline budget target",
                    "impact": "-500 SEK/mo",
                    "explanation": "Set monthly savings target to 3000 SEK to match your financial goals."
                })
                
        return {
            "month": month,
            "overall_actual": round(overall_actual, 2),
            "overall_projected": round(overall_projected, 2),
            "overall_limit": round(sum(budgets.values()), 2),
            "income": income,
            "savings_goal": savings_goal,
            "too_early_to_project": too_early_to_project,
            "category_bars": sorted(category_bars, key=lambda x: x["actual"], reverse=True),
            "actions": actions[:3]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
        
    try:
        # Construct types.Content for LLM interaction
        message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=req.message)]
        )
        
        # Run local chat agent with StreamingMode.NONE (non-streaming)
        events = []
        async for event in chat_runner.run_async(
            new_message=message,
            user_id="dashboard_user",
            session_id=req.session_id,
            run_config=RunConfig(streaming_mode=StreamingMode.NONE)
        ):
            events.append(event)
            
        # Extract response from the final content-bearing event to avoid duplication
        answer = ""
        for event in reversed(events):
            if event.content and event.content.parts:
                parts = [p.text for p in event.content.parts if p.text]
                if parts:
                    answer = "".join(parts).strip()
                    break
                    
        if not answer:
            answer = "I processed your request, but did not generate a text response."
            
        return {"response": answer}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent runtime error: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
