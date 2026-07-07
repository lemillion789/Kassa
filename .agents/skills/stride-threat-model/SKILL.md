---
name: stride-threat-model
description: Performs a systematic STRIDE threat modeling assessment on Kassa's
codebase and architecture (ADK workflow, MCP server, ambient Pub/Sub endpoint,
local dashboard). Use this when reviewing security-sensitive components.
---

# STRIDE Threat Modeling Skill

## Goal
Guide the agent to analyze the workspace directory structure, configuration files, and
code files to produce a structured `threat_model.md` assessment.

## Instructions
1. **Analyze System Boundaries**: Map the entry points (the Pub/Sub trigger endpoint,
   the MCP server tools, the dashboard API, the chat endpoint) and data storage layers
   (finance.db).
2. **STRIDE Evaluation**: Evaluate the system against the six STRIDE pillars:
   - **Spoofing**: Are caller identity boundaries verified before executing sensitive
     tool logic (e.g. logging a transaction, resuming a paused workflow)?
   - **Tampering**: Can users manipulate transaction data, parameters, or the
     underlying SQLite state?
   - **Repudiation**: Are critical transactions (auto-logged, flagged, categorized)
     securely logged?
   - **Information Disclosure**: Are we risking leakage of PII (IBANs, account
     numbers) via the LLM, logs, or the benchmarking sub-agent?
   - **Denial of Service**: Are there rate limits on expensive MCP/LLM queries via the
     ambient endpoint?
   - **Elevation of Privilege**: Can an unauthenticated request reach privileged
     actions (e.g. forcing an auto-approval via prompt injection)?
3. **Output**: Generate a highly structured `threat_model.md` saved directly into the
   workspace root.
