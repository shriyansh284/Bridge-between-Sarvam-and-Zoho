"""
Local stand-in for Zoho Mail, to try the whole email flow without a real mailbox.

    uvicorn tools.mock_zoho:app --port 9100

.env for local testing:
    ZOHO_ACCOUNTS_URL=http://127.0.0.1:9100
    ZOHO_MAIL_API_URL=http://127.0.0.1:9100
    ZOHO_CLIENT_ID=x  ZOHO_CLIENT_SECRET=x  ZOHO_REFRESH_TOKEN=x  ZOHO_FROM_ADDRESS=calling@ams.test

"Send" a client email:
    curl -F file=@candidates.xlsx -F from=hr@client.com -F subject="Calling request" \
         http://127.0.0.1:9100/_inject
See what the system replied:
    curl http://127.0.0.1:9100/_outbox
"""
import itertools
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile

app = FastAPI(title="Mock Zoho Mail")
_ids = itertools.count(1_700_000_000_000_000_001)
MESSAGES: dict[str, dict] = {}
UPLOADS: dict[str, dict] = {}
OUTBOX: list[dict] = []
ACCOUNT, FOLDER = "5550001", "9000001"


def _ok(data=None):
    return {"status": {"code": 200, "description": "success"}, "data": data}


def _auth(authorization: Optional[str]):
    if not authorization or not authorization.startswith("Zoho-oauthtoken "):
        raise HTTPException(401, "missing token")


@app.post("/oauth/v2/token")
async def token():
    return {"access_token": "mock-token", "expires_in": 3600, "token_type": "Bearer"}


@app.get("/api/accounts")
async def accounts(authorization: str = Header(None)):
    _auth(authorization)
    return _ok([{"accountId": ACCOUNT, "primaryEmailAddress": "calling@ams.test"}])


@app.get("/api/accounts/{aid}/folders")
async def folders(aid: str, authorization: str = Header(None)):
    _auth(authorization)
    return _ok([{"folderId": FOLDER, "folderName": "Inbox", "folderType": "Inbox"},
                {"folderId": "9000002", "folderName": "Sent", "folderType": "Sent"}])


@app.get("/api/accounts/{aid}/messages/view")
async def view(aid: str, request: Request, authorization: str = Header(None)):
    _auth(authorization)
    q = request.query_params
    out = []
    for m in MESSAGES.values():
        if q.get("status") == "unread" and m["read"]:
            continue
        if q.get("attachedMails") == "true" and not m["attachments"]:
            continue
        out.append({"messageId": m["messageId"], "folderId": FOLDER, "fromAddress": m["from"],
                    "subject": m["subject"], "threadId": m["messageId"], "receivedTime": "1763500000000",
                    "hasAttachment": "1"})
    return _ok(out)


@app.get("/api/accounts/{aid}/folders/{fid}/messages/{mid}/attachmentinfo")
async def attachmentinfo(aid: str, fid: str, mid: str, authorization: str = Header(None)):
    _auth(authorization)
    m = MESSAGES[mid]
    return _ok({"attachments": [{"attachmentId": a["id"], "attachmentName": a["name"], "attachmentSize": len(a["data"])}
                                for a in m["attachments"]], "messageId": mid})


@app.get("/api/accounts/{aid}/folders/{fid}/messages/{mid}/attachments/{att}")
async def attachment(aid: str, fid: str, mid: str, att: str, authorization: str = Header(None)):
    _auth(authorization)
    a = next(a for a in MESSAGES[mid]["attachments"] if a["id"] == att)
    return Response(a["data"], media_type="application/octet-stream")


@app.post("/api/accounts/{aid}/messages/attachments")
async def upload(aid: str, attach: UploadFile = File(...), authorization: str = Header(None)):
    _auth(authorization)
    key = f"/Mail/{next(_ids)}-{attach.filename}"
    UPLOADS[key] = {"name": attach.filename, "data": await attach.read()}
    return _ok([{"storeName": "mockstore", "attachmentName": attach.filename, "attachmentPath": key}])


def _store(body: dict, reply_to: Optional[str]):
    atts = [{"name": a["attachmentName"], "path": a["attachmentPath"]} for a in body.get("attachments", [])]
    OUTBOX.append({"in_reply_to": reply_to, "action": body.get("action"), "from": body.get("fromAddress"),
                   "to": body.get("toAddress"), "cc": body.get("ccAddress"), "subject": body.get("subject"),
                   "content": body.get("content"), "attachments": atts})


@app.post("/api/accounts/{aid}/messages/{mid}")
async def reply(aid: str, mid: str, request: Request, authorization: str = Header(None)):
    _auth(authorization)
    body = await request.json()
    if body.get("action", "").lower() != "reply" or mid not in MESSAGES:
        raise HTTPException(400, "bad reply")
    _store(body, mid)
    return _ok({"messageId": str(next(_ids))})


@app.post("/api/accounts/{aid}/messages")
async def send(aid: str, request: Request, authorization: str = Header(None)):
    _auth(authorization)
    _store(await request.json(), None)
    return _ok({"messageId": str(next(_ids))})


@app.put("/api/accounts/{aid}/updatemessage")
async def update(aid: str, request: Request, authorization: str = Header(None)):
    _auth(authorization)
    body = await request.json()
    if body.get("mode") == "markAsRead":
        for mid in body.get("messageId", []):
            if str(mid) in MESSAGES:
                MESSAGES[str(mid)]["read"] = True
    return _ok()


# ---- test helpers -------------------------------------------------------
@app.post("/_inject")
async def inject(file: UploadFile = File(...), sender: str = Form("hr@client.test", alias="from"),
                 subject: str = Form("AMS Calling Request")):
    mid = str(next(_ids))
    MESSAGES[mid] = {"messageId": mid, "from": f'"Client HR" <{sender}>', "subject": subject, "read": False,
                     "attachments": [{"id": str(next(_ids)), "name": file.filename, "data": await file.read()}]}
    return {"messageId": mid}


@app.get("/_outbox")
async def outbox():
    return OUTBOX


@app.get("/_upload")
async def get_upload(path: str):
    return Response(UPLOADS[path]["data"], media_type="application/octet-stream")


@app.get("/_messages")
async def messages():
    return [{k: v for k, v in m.items() if k != "attachments"} for m in MESSAGES.values()]
