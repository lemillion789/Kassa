# STRIDE Threat Model Assessment: Kassa

## 1. System Boundaries & Architecture
- **Entry Points:**
  - Ambient Pub/Sub endpoint (`POST /` in `finance_agent/fast_api_app.py`)
  - Interactive Dashboard API (`GET /`, `GET /api/insights`, `POST /api/action`, `POST /api/chat` in `dashboard/main.py`)
  - FastMCP Server Tools (invoked locally by the ADK workflow)
- **Data Storage:** SQLite Database (`finance.db`)
- **External Dependencies:** Google Gemini API (LLM)

## 2. STRIDE Evaluation

### Spoofing (Authenticity)
- **Finding:** The system lacks authentication on both the Pub/Sub ingestion endpoint and the dashboard API. 
- **Impact:** Any user or process with network access to the local ports (8080 or 8090) can spoof incoming transactions or simulate human approvals on flagged items.
- **Recommendation:** Implement robust authentication mechanisms. For the Pub/Sub endpoint, validate Google Pub/Sub JWT tokens. For the dashboard, consider lightweight auth or ensuring the service is bound strictly to `localhost` in production.

### Tampering (Integrity)
- **Finding:** Without authentication and authorization boundaries, an attacker can modify transactions, approve flagged items, or alter configuration. Additionally, `finance.db` is an unencrypted SQLite database.
- **Impact:** Malicious actors could manipulate spending baselines, alter financial history, or modify budget rules directly in the database if they gain local filesystem access.
- **Recommendation:** Restrict API access. Consider encrypting the SQLite database at rest (e.g., using SQLCipher) if running on a shared host.

### Repudiation (Non-repudiability)
- **Finding:** While the system logs transaction outcomes to the database (`record_outcome`), there is no reliable tracking of *who* performed a manual action (like an approval) because there are no user identities or sessions.
- **Impact:** If an anomalous transaction is incorrectly approved via the dashboard, there is no audit trail tying the approval to a specific user or IP address.
- **Recommendation:** Introduce user sessions and bind human-in-the-loop approvals to specific authenticated users in the application's audit logs.

### Information Disclosure (Confidentiality)
- **Finding:** The system leverages a `sanitize_and_check_security` node to redact IBANs and account numbers before data is sent to the LLM. However, if the sanitization logic relies solely on static rules or regex, it may miss edge cases (e.g., non-standard formatted IDs). Additionally, application logs might capture sensitive data before sanitization occurs.
- **Impact:** PII could be leaked to the external LLM provider (Gemini) or exposed in plain text in local server logs.
- **Recommendation:** Ensure standard application logging redacts PII immediately at the ingestion point. Expand sanitization capabilities to use rigorous formats and ensure the disabled benchmarking sub-agent stays isolated from sensitive descriptions.

### Denial of Service (Availability)
- **Finding:** The ambient Pub/Sub endpoint and the dashboard chat endpoint have no rate limiting configured.
- **Impact:** An attacker could flood the system with mock transactions or chat messages, exhausting Google Gemini API quotas and local compute resources.
- **Recommendation:** Implement rate limiting on the FastAPI routes (e.g., using `slowapi`) and configure strict budget alerts/quotas on the LLM API side.

### Elevation of Privilege (Authorization)
- **Finding:** The system includes prompt injection detection in the security checkpoint to prevent attackers from forcing an auto-approval (e.g., "Ignore all rules..."). However, LLM prompt injection defenses are probabilistic and can be bypassed by sophisticated payloads.
- **Impact:** An attacker could craft a transaction description that bypasses the keyword checks, manipulating the LLM into auto-logging a fraudulent transaction or potentially abusing MCP tool schemas.
- **Recommendation:** Continue employing defense-in-depth: keep the deterministic human-in-the-loop fallback for transactions above specific monetary thresholds (e.g., the existing > 5000 SEK rule) regardless of LLM confidence or routing decisions.
