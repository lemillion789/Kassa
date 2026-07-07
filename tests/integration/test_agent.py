# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import pytest
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from finance_agent.agent import root_agent


def test_agent_auto_log() -> None:
    """
    Tests that a transaction below threshold and with a known category
    is auto-categorized and logged without LLM review.
    """
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(user_id="test_user", app_name="test")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="test")

    # Transaction under 500 SEK with known category "Groceries"
    transaction_payload = {
        "data": {
            "amount": 250.0,
            "merchant": "ICA Maxi",
            "category": "Groceries",
            "description": "Weekly grocery run",
            "date": "2026-07-07"
        }
    }

    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=json.dumps(transaction_payload))]
    )

    events = list(
        runner.run(
            new_message=message,
            user_id="test_user",
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )

    assert len(events) > 0, "Expected events from the workflow run"
    
    final_output = None
    for event in reversed(events):
        if event.output is not None:
            final_output = event.output
            break
            
    assert final_output is not None, "Expected a final output event"
    assert final_output["status"] == "auto_logged"
    assert final_output["transaction"]["amount"] == 250.0


@pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY is not set for integration tests"
)
def test_agent_review_required() -> None:
    """
    Tests that a transaction above threshold triggers review and requests input.
    """
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(user_id="test_user", app_name="test")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="test")

    # Transaction above 500 SEK
    transaction_payload = {
        "data": {
            "amount": 1200.0,
            "merchant": "Apple Store",
            "category": "Electronics",
            "description": "New headphones",
            "date": "2026-07-07"
        }
    }

    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=json.dumps(transaction_payload))]
    )

    events = list(
        runner.run(
            new_message=message,
            user_id="test_user",
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )

    assert len(events) > 0

    # The workflow should pause and yield RequestInput
    has_request_input = False
    for event in events:
        # Check if the runner returned a request input (HITL)
        if hasattr(event, "interrupt_ids") and event.interrupt_ids:
            has_request_input = True
            break
            
    assert has_request_input or any(hasattr(e, "message") and "confirm_category" in str(e) for e in events)
