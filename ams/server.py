"""
The always-on backend.

    uvicorn ams.server:app --host 0.0.0.0 --port 8000      (run with ONE worker)

Routes
  POST /webhooks/sarvam/{WEBHOOK_SECRET}   Sarvam posts each call result here (only with a public address)
  GET  /rec/{token}                        signed recording link used in the result Excel (only with a public address)
  GET  /health                             status for monitoring

No public address is needed: without one, results are fetched from Sarvam (ams/results.py) and
recording links in the Excel are Sarvam's own signed links. Bind to 127.0.0.1 in that case.

It also runs the scheduler and the SFTP folder watcher in background threads
(set BACKGROUND_IN_SERVER=false to run `python -m ams.scheduler` separately).
"""
from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool

from .config import settings
from .links import read_token
from .results import process_result
from .sarvam import fetch_recording, fetch_recording_url
from .scheduler import Background
from .store import get_status, load_campaign

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    bg = None
    if os.environ.get("BACKGROUND_IN_SERVER", "true").lower() in ("1", "true", "yes"):
        bg = Background()
        bg.start()
    yield
    if bg:
        bg.stop()


app = FastAPI(title="AMS calling automation", lifespan=lifespan, docs_url=None, redoc_url=None)


def _process(payload: dict) -> str:
    return process_result(payload, "webhook")


@app.post("/webhooks/sarvam/{token}")
async def sarvam_webhook(token: str, request: Request):
    if not settings.webhook_secret or not hmac.compare_digest(token, settings.webhook_secret):
        raise HTTPException(status_code=404)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    outcome = await run_in_threadpool(_process, payload)
    return {"ok": True, "outcome": outcome}


def _recording(cid: str, row_id: str, ref: str):
    camp = load_campaign(cid)
    if not camp:
        return None
    row = next((r for r in camp["rows"] if r["row_id"] == row_id), None)
    att = next((a for a in (row or {}).get("attempts", []) if a["ref"] == ref), None)
    if not att:
        return None
    if att.get("recording_url"):
        return fetch_recording_url(att["recording_url"])
    return fetch_recording(att.get("interaction_id"), att.get("app_id"))


@app.get("/rec/{token}")
async def recording(token: str):
    found = read_token(token)
    if not found:
        raise HTTPException(status_code=404, detail="This recording link is invalid or has expired.")
    try:
        audio = await run_in_threadpool(_recording, *found)
    except Exception:
        log.exception("Recording fetch failed")
        raise HTTPException(status_code=502, detail="The recording couldn't be loaded right now. Try again later.")
    if not audio:
        raise HTTPException(status_code=404, detail="No recording is available for this call yet.")
    return Response(audio, media_type="audio/wav",
                    headers={"Content-Disposition": f'inline; filename="{found[0]}_{found[1]}.wav"',
                             "Cache-Control": "private, max-age=3600"})


@app.get("/health")
async def health():
    status = get_status()
    return {"ok": True, "scheduler_heartbeat": status.get("scheduler_heartbeat"),
            "drop_last_check": status.get("drop_last_check"), "last_webhook": status.get("last_webhook"),
            "result_poll": status.get("result_poll"), "calls_paused": status.get("calls_paused")}
