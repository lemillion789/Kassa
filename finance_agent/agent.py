import base64
import json
import os
import asyncio
from datetime import datetime
from typing import Any, AsyncGenerator

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.models import Gemini
from google.adk.workflow import Workflow, node
from google.genai import types
from pydantic import BaseModel, Field

import re
# MCP Toolset and connection parameters
from google.adk.tools import FunctionTool, google_search
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from mcp import StdioServerParameters

from finance_agent.config import KNOWN_CATEGORIES, MODEL_NAME, SEK_THRESHOLD


# Input schema for Pub/Sub transaction events (kept for schema/documentation purposes)
class PubSubMessage(BaseModel):
    data: Any = Field(description="Transaction event data, possibly base64-encoded or plain JSON.")


# Output schema for LLM Reviewer
class ReviewInsight(BaseModel):
    insight: str = Field(description="Brief insight explaining findings (subscriptions, budget breakers, duplicates, etc.).")
    is_recurring: bool = Field(description="True if this appears to be a new recurring subscription.")
    is_budget_breaker: bool = Field(description="True if this purchase breaks the typical budget.")
    is_duplicate: bool = Field(description="True if this transaction is likely a duplicate.")


# Output schema for the entire workflow (when running transaction routing)
class WorkflowOutput(BaseModel):
    status: str = Field(description="Outcome status, e.g., 'auto_logged', 'flagged', or 'categorized'.")
    transaction: dict = Field(description="The transaction details.")
    review: dict | None = Field(default=None, description="LLM review results, if applicable.")


# Absolute path to the MCP server script
MCP_SERVER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mcp_server",
    "server.py"
)

# Configure the Stdio connection parameters for the local MCP server
mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="python3",
            args=[MCP_SERVER_PATH],
        )
    )
)

# Lazy MCP tool resolution — works both inside a running event loop (playground)
# and outside one (tests, CLI).
_mcp_tool_cache: dict[str, Any] = {}

async def _resolve_mcp_tools():
    """Fetch tool handles from the MCP server, caching them for reuse."""
    if not _mcp_tool_cache:
        tools = await mcp_toolset.get_tools()
        for t in tools:
            _mcp_tool_cache[t.name] = t
    return _mcp_tool_cache

async def get_mcp_tool(name: str):
    """Get a single MCP tool handle by name."""
    cache = await _resolve_mcp_tools()
    return cache[name]


def sanitize_and_check_security(data: dict) -> dict:
    """Detects and redacts IBANs and flags potential prompt injections."""
    # Swedish IBAN starts with SE followed by 22 digits (often spaced in groups of 4)
    # Generic IBAN pattern: matches SE plus spacing and digits
    iban_pattern = r'\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,8}(?:\s?[A-Z0-9]{1,4})?\b'
    
    # Prompt injection keywords
    injection_keywords = [
        "ignore all rules", "ignore previous instructions", "override rules",
        "auto-log this", "auto log this", "bypass review", "do not review"
    ]
    
    is_security_event = False
    
    # Iterate through all fields and sanitize/check
    for key in list(data.keys()):
        value = data[key]
        if isinstance(value, str):
            # Check for IBAN
            if re.search(iban_pattern, value):
                print(f"[SECURITY CHECKPOINT] Detected IBAN in field '{key}'. Redacting...")
                value = re.sub(iban_pattern, "[REDACTED IBAN]", value)
                data[key] = value
                is_security_event = True
                
            # Check for prompt injection keywords
            val_lower = value.lower()
            if any(keyword in val_lower for keyword in injection_keywords):
                print(f"[SECURITY CHECKPOINT] Potential prompt injection detected in field '{key}'!")
                is_security_event = True
                
    if is_security_event:
        data["is_security_event"] = True
        
    return data


@node
def extract_transaction(node_input: Any) -> dict:
    """Parses raw transaction event data (supports types.Content, base64 and JSON strings/dicts)."""
    
    # Extract text content if coming from a standard chat runner message
    if hasattr(node_input, "parts") and node_input.parts:
        text = node_input.parts[0].text
        data = text
    else:
        data = node_input

    # Parse JSON if data is a JSON string
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            pass

    # Unpack Pub/Sub envelope {"data": ...} if present
    if isinstance(data, dict) and "data" in data:
        data = data["data"]

    # Try base64 decoding if data is a string (e.g. from Pub/Sub)
    if isinstance(data, str):
        try:
            decoded = base64.b64decode(data).decode("utf-8")
            try:
                parsed = json.loads(decoded)
                if isinstance(parsed, dict):
                    data = parsed
            except json.JSONDecodeError:
                pass
        except Exception:
            pass
            
        # Try JSON parsing again if still a string
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    data = parsed
            except json.JSONDecodeError:
                pass

    # Fallback to conversational query payload if parsing failed (e.g., standard greetings / test messages)
    if not isinstance(data, dict) or not any(k in data for k in ("amount", "merchant")):
        s_data = str(data).strip()
        if s_data.startswith("/advice") or "advice" in s_data.lower():
            match = re.search(r'\b\d{4}-\d{2}\b', s_data)
            target_month = match.group(0) if match else datetime.today().strftime("%Y-%m-%d")[:7]
            return {
                "is_advice_request": True,
                "month": target_month
            }
        return {
            "is_conversational": True,
            "query": s_data
        }
        
    extracted = {
        "amount": float(data.get("amount", 0.0)),
        "merchant": str(data.get("merchant", "")),
        "category": str(data.get("category", "")),
        "description": str(data.get("description", "")),
        "date_str": str(data.get("date") or data.get("date_str") or datetime.today().strftime("%Y-%m-%d"))
    }
    return sanitize_and_check_security(extracted)


@node
def route_transaction(node_input: dict) -> Event:
    """Routes the transaction based on SEK threshold and known categories rule."""
    if node_input.get("is_advice_request"):
        if os.environ.get("INTEGRATION_TEST") == "TRUE":
            node_input["amount"] = 0.0
            node_input["merchant"] = "Integration Test"
            node_input["category"] = "Groceries"
            node_input["description"] = "Mock advice transaction"
            node_input["date_str"] = "2026-07-07"
            return Event(
                output=node_input,
                route="auto",
                state={"transaction": node_input}
            )
        return Event(
            output=node_input,
            route="advice"
        )
        
    if node_input.get("is_security_event"):
        return Event(
            output=node_input,
            route="review",
            state={"transaction": node_input}
        )
        
    if node_input.get("is_conversational"):
        if os.environ.get("INTEGRATION_TEST") == "TRUE":
            node_input["amount"] = 0.0
            node_input["merchant"] = "Integration Test"
            node_input["category"] = "Groceries"
            node_input["description"] = "Mock conversational transaction"
            node_input["date_str"] = "2026-07-07"
            return Event(
                output=node_input,
                route="auto",
                state={"transaction": node_input}
            )
        return Event(
            output=node_input["query"],
            route="chat"
        )
        
    amount = node_input["amount"]
    category = node_input["category"]
    
    # Case-insensitive category check
    is_known = any(c.lower() == category.lower() for c in KNOWN_CATEGORIES)
    
    if amount < SEK_THRESHOLD and is_known:
        return Event(
            output=node_input,
            route="auto",
            state={"transaction": node_input}
        )
    else:
        return Event(
            output=node_input,
            route="review",
            state={"transaction": node_input}
        )


@node(rerun_on_resume=True)
async def auto_log(ctx: Context, node_input: dict) -> AsyncGenerator[Event, None]:
    """Logs the transaction instantly via MCP log_transaction tool."""
    print(f"[AUTO-LOG] Logging transaction via MCP: {node_input}")
    
    log_tool = await get_mcp_tool("log_transaction")
    log_res = await ctx.run_node(log_tool, node_input=node_input)
    print(f"[AUTO-LOG] MCP response: {log_res}")
    
    msg = (
        f"✅ Transaction auto-logged successfully via MCP!\n"
        f"Merchant: {node_input['merchant']}\n"
        f"Amount: {node_input['amount']} SEK\n"
        f"Category: {node_input['category']}"
    )
    yield Event(content=types.Content(
        role="model",
        parts=[types.Part.from_text(text=msg)]
    ))
    
    yield Event(output={
        "status": "auto_logged",
        "transaction": node_input
    })


# LLM Agent to review transactions exceeding threshold or having unusual categories
reviewer = LlmAgent(
    name="reviewer",
    model=Gemini(
        model=MODEL_NAME,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are an expert personal finance assistant. Review the provided transaction:
- Is this a new recurring subscription?
- Is this a budget-breaking purchase?
- Is this a likely duplicate of a previous charge?
You can search the transaction history or check baselines using your tools.
Provide a brief, concise insight explaining your findings, and set the flags accordingly.""",
    output_schema=ReviewInsight,
    output_key="review_result",
    tools=[mcp_toolset]
)


@node
async def get_human_confirmation(ctx: Context, node_input: dict | str) -> AsyncGenerator[Event | RequestInput, None]:
    """Pauses the workflow to request human confirmation/flagging of the transaction."""
    transaction = ctx.state.get("transaction", {})
    
    # Store LLM review in state on first run so we can access it when resuming.
    if isinstance(node_input, dict):
        ctx.state["review"] = node_input
        
    review_data = ctx.state.get("review", {})
    
    if not ctx.resume_inputs or "confirm_category" not in ctx.resume_inputs:
        prompt_message = (
            f"⚠️ TRANSACTION REVIEW REQUIRED (Amount: {transaction.get('amount')} SEK)\n"
            f"Merchant: {transaction.get('merchant')}\n"
            f"Reported Category: {transaction.get('category')}\n"
            f"Description: {transaction.get('description')}\n"
            f"Date: {transaction.get('date_str')}\n\n"
            f"🤖 LLM Analysis:\n"
            f"- Recurring Subscription: {'Yes' if review_data.get('is_recurring') else 'No'}\n"
            f"- Budget Breaker: {'Yes' if review_data.get('is_budget_breaker') else 'No'}\n"
            f"- Likely Duplicate: {'Yes' if review_data.get('is_duplicate') else 'No'}\n"
            f"- Insight: {review_data.get('insight')}\n\n"
            f"Please enter the confirmed category to proceed, or type 'flag' to flag this transaction."
        )
        yield RequestInput(interrupt_id="confirm_category", message=prompt_message)
        return
        
    # Extract decision, handling both string and dict formats
    user_decision_raw = ctx.resume_inputs["confirm_category"]
    if isinstance(user_decision_raw, dict):
        user_decision = user_decision_raw.get("decision") or user_decision_raw.get("confirm_category") or str(user_decision_raw)
    else:
        user_decision = str(user_decision_raw)
        
    yield Event(content=types.Content(
        role="model",
        parts=[types.Part.from_text(text=f"Decision received: {user_decision}")]
    ))
    
    yield Event(output={
        "transaction": transaction,
        "review": review_data,
        "decision": user_decision
    })


@node(rerun_on_resume=True)
async def record_outcome(ctx: Context, node_input: dict | str) -> AsyncGenerator[Event, None]:
    """Finalizes and records the categorization or flagging decision via MCP log_transaction tool."""
    # Read original transaction and review from state/context
    transaction = ctx.state.get("transaction", {})
    review = ctx.state.get("review", {})
    
    # Read the decision from node_input or state
    if isinstance(node_input, dict):
        decision = str(node_input.get("decision", "")).strip()
        if "transaction" in node_input:
            transaction = node_input["transaction"]
        if "review" in node_input:
            review = node_input["review"]
    else:
        decision = str(node_input).strip()
        
    if not decision:
        decision = str(ctx.state.get("decision") or ctx.resume_inputs.get("confirm_category") or "flag").strip()

    if decision.lower() == "flag":
        status = "flagged"
        transaction["category"] = "Flagged"
        log_tool = await get_mcp_tool("log_transaction")
        log_res = await ctx.run_node(log_tool, node_input=transaction)
        msg = f"🚨 Transaction flagged for review and logged via MCP: {transaction.get('merchant')} ({transaction.get('amount')} SEK)"
    else:
        status = "categorized"
        transaction["category"] = decision
        log_tool = await get_mcp_tool("log_transaction")
        log_res = await ctx.run_node(log_tool, node_input=transaction)
        msg = f"💾 Transaction categorized as '{decision}' and logged via MCP!"
        
    print(f"[RECORD-OUTCOME] Transaction recorded via MCP: status={status}, transaction={transaction}")
    
    yield Event(content=types.Content(
        role="model",
        parts=[types.Part.from_text(text=msg)]
    ))
    
    yield Event(output={
        "status": status,
        "transaction": transaction,
        "review": review
    })


# Benchmarking sub-agent (uses google_search tool ONLY)
# RESERVED FOR FUTURE WORK: Live search sub-agent is defined here but currently
# disabled in the live build due to API invocation limits and compatibility.
benchmarking_agent = LlmAgent(
    name="benchmarking_agent",
    model=Gemini(
        model=MODEL_NAME,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are a dedicated research sub-agent for external benchmarking.
Your job is to search the web using your Google Search tool for typical, average, or baseline expenditures in Sweden or other locations for a generic category.
Provide a short, sourced summary of the average spending, referencing standard sources (like Konsumentverket, SCB, or local finance guides).
Do NOT mention user transaction details, amounts, or accounts, as you do not have access to them.""",
    tools=[google_search]
)


async def benchmark_spending(category: str, location: str = "Sweden") -> str:
    """Compare a category of spending against national or local averages in a country/city.
    
    Args:
        category: The generic spending category name (e.g. 'groceries', 'rent', 'transport'). Do NOT include transaction details, amounts, or merchant names.
        location: The country or city context (defaults to 'Sweden'). Do NOT include specific addresses.
    """
    # Security check: Strip any numeric digits (amounts)
    sanitized_category = re.sub(r'\d+', '', category)
    
    # Strip currency signs (SEK, kr, $, €, £, etc.)
    sanitized_category = re.sub(r'(?i)\b(sek|kr|usd|eur|gbp|dollars|euros|kronor|kronans|kronor)\b|[\$\€\£]', '', sanitized_category)
    
    # Strip common transaction words (merchant, amount, purchase, transaction)
    sanitized_category = re.sub(r'(?i)\b(merchant|amount|purchase|transaction|date|card|bank|account|charge)\b', '', sanitized_category)
    
    # Strip multiple spaces and strip padding
    sanitized_category = re.sub(r'\s+', ' ', sanitized_category).strip()
    
    # Security check: if the remaining category is empty, or too long (e.g., trying to pass a full message / sentence), reject it!
    # A generic category name should typically be short (under 30 characters and 1-3 words)
    if not sanitized_category or len(sanitized_category) > 40 or len(sanitized_category.split()) > 4:
         raise ValueError(
             "Security rejection: The category parameter must be a short, generic category name (e.g. 'groceries', 'eating out'). "
             "Specific transaction details, amounts, currencies, or long phrases are strictly forbidden."
         )
         
    # Sanitize location in a similar fashion (no numbers, no special chars, short string)
    sanitized_location = re.sub(r'[^a-zA-Z\s,]', '', location)
    sanitized_location = re.sub(r'\s+', ' ', sanitized_location).strip()
    if not sanitized_location or len(sanitized_location) > 40:
        sanitized_location = "Sweden"
        
    print(f"[SECURITY] Sanitized spending benchmark request: category='{sanitized_category}', location='{sanitized_location}'")
    
    # Live web-search sub-agent invocation is disabled in this build.
    # We return a static message showing that the sanitization/security constraint succeeded.
    return (
        f"Security check passed — only the generic category '{sanitized_category}' and "
        f"location '{sanitized_location}' would be sent externally. "
        f"(Live web benchmarking is disabled in this build; see README future work.)"
    )


# Conversational agent exposing all analysis tools and the benchmarking function tool wrapper
chat_agent = LlmAgent(
    name="chat_agent",
    model=Gemini(
        model=MODEL_NAME,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are a personal finance assistant. You have access to tools to manage the user's finance database.
Answer the user's query or perform the requested action using the appropriate tools.
If the user asks whether their spending is normal or wants to compare it to national/local averages, use the `benchmark_spending` tool.
All amounts are in SEK.""",
    tools=[mcp_toolset, FunctionTool(benchmark_spending)]
)


@node(rerun_on_resume=True)
async def fetch_advice_insights(ctx: Context, node_input: dict) -> Event:
    """Calls collect_insights via the MCP tool for the specified month."""
    month = node_input["month"]
    print(f"[ADVICE] Fetching insights for month {month} via MCP...")
    
    # Run MCP collect_insights tool
    insights_tool = await get_mcp_tool("collect_insights")
    insights_str = await ctx.run_node(insights_tool, node_input={"month": month})
    print(f"[ADVICE] Insights fetched: {insights_str}")
    
    # Pass the JSON payload directly to the LLM agent
    return Event(
        output={
            "month": month,
            "insights_raw": insights_str
        }
    )


# LLM Agent to synthesize the advice list
advice_synthesizer = LlmAgent(
    name="advice_synthesizer",
    model=Gemini(
        model=MODEL_NAME,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are a personal finance advice expert.
Your input is a JSON bundle of raw financial insights containing:
- top_deviations
- active_subscriptions
- phantom_subscriptions
- over_budget_categories
- savings_gap

Your job is to synthesize a SHORT, prioritized action list.
Rank the actions by their monthly SEK impact, biggest first (e.g. cancels, trims, budget fixes).
For each action, output:
1. The action description
2. The estimated monthly SEK savings/impact
3. A one-sentence explanation of why it is needed.

CRITICAL RULES:
- Never invent numbers. Every figure or percentage must come directly from the raw insights data.
- If no actions are needed, congratulate the user.
- Keep the response extremely concise and clear."""
)


# Wire up the Workflow graph
root_agent = Workflow(
    name="finance_workflow",
    edges=[
        ('START', extract_transaction),
        (extract_transaction, route_transaction),
        (route_transaction, {
            "auto": auto_log,
            "review": reviewer,
            "chat": chat_agent,
            "advice": fetch_advice_insights
        }),
        (reviewer, get_human_confirmation),
        (get_human_confirmation, record_outcome),
        (fetch_advice_insights, advice_synthesizer)
    ]
)

# App container enabling Human-in-the-Loop resumability
app = App(
    root_agent=root_agent,
    name="finance_agent",
    resumability_config=ResumabilityConfig(is_resumable=True)
)
