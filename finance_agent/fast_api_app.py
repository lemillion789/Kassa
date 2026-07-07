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

import contextlib
import os
from collections.abc import AsyncIterator

import google.auth
from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from google.cloud import logging as google_cloud_logging

from finance_agent.app_utils import services
from finance_agent.app_utils.a2a import attach_a2a_routes
from finance_agent.app_utils.telemetry import setup_telemetry
from finance_agent.app_utils.typing import Feedback

import json
import logging

class FallbackLogger:
    def __init__(self, name: str):
        self._logger = logging.getLogger(name)
        
    def log_struct(self, data: dict, severity: str = "INFO") -> None:
        self._logger.info(f"[{severity}] {json.dumps(data)}")

load_dotenv()
setup_telemetry()

project_id = None
logger = FallbackLogger(__name__)

if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() == "true":
    try:
        _, project_id = google.auth.default()
        logging_client = google_cloud_logging.Client()
        logger = logging_client.logger(__name__)
    except Exception as e:
        print(f"Warning: Could not initialize Google Cloud Logging: {e}")
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from finance_agent.agent import app as adk_app
    from finance_agent.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=False,
    lifespan=lifespan,
)
app.title = "ambient-finance-agent"
app.description = "API for interacting with the Agent ambient-finance-agent"


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    logger.log_struct(feedback.model_dump(), severity="INFO")
    return {"status": "success"}


from fastapi import Request, Response, HTTPException
import base64

@app.post("/")
@app.post("/pubsub")
async def handle_pubsub(request: Request):
    """Handles Google Cloud Pub/Sub push trigger messages."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
        
    pubsub_msg = body.get("message")
    if not pubsub_msg:
        raise HTTPException(status_code=400, detail="Missing 'message' field")
        
    data_raw = pubsub_msg.get("data")
    if not data_raw:
        raise HTTPException(status_code=400, detail="Missing 'data' field in pubsub message")
        
    # Decode base64 if it is base64 encoded
    try:
        # Check if it's base64 encoded by trying to decode it
        decoded_bytes = base64.b64decode(data_raw, validate=True)
        data_str = decoded_bytes.decode("utf-8")
    except Exception:
        # If not valid base64, assume plain text/JSON string
        if isinstance(data_raw, str):
            data_str = data_raw
        else:
            data_str = json.dumps(data_raw)
            
    # Try parsing the decoded data string as JSON
    try:
        transaction_payload = json.loads(data_str)
    except Exception:
        # If it's not a JSON string, wrap it or use it as is
        transaction_payload = data_str

    # Extract fully-qualified subscription name and normalize it
    fq_subscription = body.get("subscription", "projects/local/subscriptions/ambient-finance-sub")
    # Normalize e.g. "projects/my-project/subscriptions/my-sub" -> "my-sub"
    session_id = fq_subscription.split("/")[-1]
    
    # Run the transaction through the workflow runner
    runner = app.state.runner
    
    from google.genai import types
    from google.adk.agents.run_config import RunConfig, StreamingMode
    
    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=json.dumps({"data": transaction_payload}))]
    )
    
    events = []
    async for event in runner.run_async(
        new_message=message,
        user_id="pubsub_trigger",
        session_id=session_id,
        run_config=RunConfig(streaming_mode=StreamingMode.SSE)
    ):
        events.append(event)
        
    final_output = None
    interrupt_required = False
    for event in reversed(events):
        if event.output is not None:
            final_output = event.output
            break
        # Check if human review is requested (HITL pause)
        if hasattr(event, "interrupt_ids") and event.interrupt_ids:
            interrupt_required = True
        elif hasattr(event, "message") and "confirm_category" in str(event):
            interrupt_required = True
            
    status_code = 200
    if interrupt_required:
        status_code = 202  # Accepted (requires human interaction)
        response_data = {
            "status": "pending_human_review",
            "session_id": session_id,
            "message": "Transaction requires human confirmation/flagging."
        }
    elif final_output:
        response_data = final_output
    else:
        response_data = {"status": "processed", "events_count": len(events)}
        
    return Response(content=json.dumps(response_data), media_type="application/json", status_code=status_code)


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
