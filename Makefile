.PHONY: install playground test seed lint clean

# ──────────────────────────────────────────────
# Ambient Finance Agent — Local Dev Makefile
# ──────────────────────────────────────────────

# Install all project dependencies (prod + dev) via uv
install:
	uv sync --all-groups
	@echo "✅ Dependencies installed."

# Launch the ADK playground web UI (http://localhost:8000)
playground:
	uv run agents-cli playground

# Launch the FastAPI web service on port 8080
run:
	PYTHONPATH=. uv run python finance_agent/fast_api_app.py

# Launch the standalone HTML dashboard on port 8090
dashboard:
	PYTHONPATH=. uv run python dashboard/main.py

# Run the full test suite
test:
	uv run pytest -v

# Seed the SQLite database with 8 months of synthetic demo data
seed:
	PYTHONPATH=. uv run python -c \
		"from mcp_server.server import seed_synthetic_data; print(seed_synthetic_data(months=8, seed=42))"

# Run baselines on the seeded data
baselines:
	PYTHONPATH=. uv run python -c \
		"from mcp_server.server import compute_baselines; print(compute_baselines())"

# Lint and format
lint:
	uv run ruff check .
	uv run ruff format --check .

# Clean up generated artifacts
clean:
	rm -f finance.db
	rm -rf __pycache__ .pytest_cache
	find . -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
	@echo "🧹 Cleaned."
