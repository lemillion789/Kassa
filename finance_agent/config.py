import os

# Configuration for ambient-finance-agent
SEK_THRESHOLD = 500.0
MODEL_NAME = "gemini-3.1-flash-lite"

# Standard categories that don't need LLM/Human review if amount is below threshold
KNOWN_CATEGORIES = {
    "Groceries",
    "Utilities",
    "Rent",
    "Salary",
    "Transport",
    "Insurance",
    "Subscriptions",
    "Dining Out",
    "Entertainment"
}
