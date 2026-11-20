"""
Local stand-in for Sarvam, so you can test the whole flow without placing
real calls or spending credits.

    uvicorn tools.mock_sarvam:app --port 9000

Then in .env:
    SARVAM_OUTBOUND_BASE=http://localhost:9000/api/outbounds
    SARVAM_ANALYTICS_BASE=http://localhost:9000/api/analytics
    PUBLIC_WEBHOOK_BASE_URL=http://localhost:8000

Each "call" reports back after MOCK_DELAY seconds with a random result
(answered / no answer / busy / failed). Set MOCK_RESULT=connected to force one,
or MOCK_RESULT=digit to decide by the phone's last digit (1 no answer, 2 busy, 3 failed).
"""
import asyncio
import io
import math
import os
import random
import struct
import uuid
import wave

import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI(title="Mock Sarvam")
DELAY = float(os.environ.get("MOCK_DELAY", "3"))
FORCED = os.environ.get("MOCK_RESULT", "")
WEIGHTS = {"connected": 60, "no_answer": 25, "busy": 10, "failed": 5}
_tasks: set = set()


def _payload(attempt_id: str, body: dict, status: str) -> dict:
    app_cfg = body["app_config"]
    variables = app_cfg.get("agent_variables") or {}
    connected = status == "connected"
    name = variables.get("name", "there")
    docs = variables.get("pending_document_names", "your documents")
    return {
        "attempt_id": attempt_id,
        "status": status,
        "channel_info": {"channel_type": "v2v", "channel_provider": "mock",
                         "agent_phone_number": app_cfg["connection_config"]["agent_phone_number"]},
        "app_id": app_cfg["app_id"],
        "app_version": app_cfg["app_version"],
        "duration": round(random.uniform(40, 180), 2) if connected else None,
        "interaction_id": f"20261119/{uuid.uuid4()}" if connected else None,
        "failure_reason": "mock: Phone number is registered under TRAI NDNC" if status == "failed" else None,
        "final_agent_variables": {**variables,  # same output names as the real AMS agent
                                  "disposition_call_disposition": "agreed_to_submit",
                                  "disposition_commitment_with_timeline": "Will upload tomorrow",
                                  "summary": f"{name} confirmed the pending documents and will upload them tomorrow.",
                                  "call_conversation_quality": "positive"} if connected else None,
        "webhook_config": body.get("webhook_config"),
        "interaction_transcript": [
            {"role": "agent", "en_text": f"Hello, am I speaking with {name}?"},
            {"role": "user", "en_text": "Yes, speaking."},
            {"role": "agent", "en_text": f"Your {docs} are still pending. When can you upload them?"},
            {"role": "user", "en_text": "I will upload them by tomorrow evening."},
        ] if connected else None,
    }


async def _report(attempt_id: str, body: dict):
    await asyncio.sleep(DELAY)
    if FORCED == "digit":  # deterministic by last digit of the number: 1 no answer, 2 busy, 3 failed
        last = body["user_config"]["user_phone_number"][-1]
        status = {"1": "no_answer", "2": "busy", "3": "failed"}.get(last, "connected")
    else:
        status = FORCED or random.choices(list(WEIGHTS), weights=list(WEIGHTS.values()))[0]
    url = (body.get("webhook_config") or {}).get("url")
    if url:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=_payload(attempt_id, body, status))


@app.post("/api/outbounds/v1/orgs/{org}/workspaces/{ws}/outbounds")
async def outbound(org: str, ws: str, request: Request):
    if not request.headers.get("x-api-key"):
        return Response(status_code=401)
    body = await request.json()
    attempt_id = str(uuid.uuid4())
    task = asyncio.create_task(_report(attempt_id, body))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"attempt_id": attempt_id}


@app.get("/api/analytics/v1/{org}/{ws}/{app_id}/recordings/{interaction_id:path}")
async def recording(org: str, ws: str, app_id: str, interaction_id: str):
    buf = io.BytesIO()  # 2-second 440 Hz tone
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 8000)))
                               for i in range(16000)))
    return Response(buf.getvalue(), media_type="audio/wav")
