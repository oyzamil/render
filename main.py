# main.py  — add first-run email-code auth (optional), keep /api/chat-completions identical
import os
import sys
import logging
import random
import string
from typing import Any, Dict

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, HTTPException, Request, Depends

app = FastAPI(title="Chat Completions Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # keep your permissive CORS
    allow_methods=["*"],
    allow_headers=["*"],
)
# =========================
# Models must be defined before routes
# =========================
class RequestCodeBody(BaseModel):
    email: EmailStr

class VerifyCodeBody(BaseModel):
    email: EmailStr
    code: str
# =========================
# Config
# =========================
PLACEHOLDER_KEY = ""
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", PLACEHOLDER_KEY)
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
SEARCHES: list[Dict[str, Any]] = []

# OPTIONAL outgoing email (set these if you want real email)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "no-reply@mactrac.app")
APP_NAME = os.getenv("APP_NAME", "MacTrac")

# Your unchanged prompt
BACKEND_PROMPT = (
    "You are a no-nonsense sports nutrition analyst.\n"
    "Return STRICT JSON only. Prefer exact values from on-page data; otherwise use USDA typical values. "
    "JUST ESTIMATE OFF THE TITLE OF THE PRODUCT IF THERES NO ESTIMATED VALUES. "
    "ESTIMATE TO THE EXACT 1s PLACE THO DONT JUST GO BY 10s\n"
    "Schema:\n"
    "{\n"
    '  "is_recipe": boolean,\n'
    '  "servings": int | null,\n'
    '  "serving_size": string | null,\n'
    '  "macros_per_serving": {"calories": int | null, "protein_g": float | null, "carbs_g": float | null, "fat_g": float | null},\n'
    '  "macros_total": {"calories": int | null, "protein_g": float | null, "carbs_g": float | null, "fat_g": float | null},\n'
    '  "confidence": float,\n'
    '  "assumptions": [string]\n'
    "}"
)
DEFAULT_MODEL = "gpt-4o-mini"

# =========================
# Logging (always visible)
# =========================
@app.get("/")
async def root():
    return {"ok": True, "service": "mactrac", "routes": [r.path for r in app.routes]}

# Helper to register both /auth/... and /api/auth/...
def _dual(path_no_api: str):
    return (f"/auth{path_no_api}", f"/api/auth{path_no_api}")

@app.post(_dual("/request-code")[0])
@app.post(_dual("/request-code")[1])
async def request_code(body: RequestCodeBody):
    _cleanup_stores()
    email = body.email.lower().strip()
    code = _mint_code()
    pending_codes[email] = (code, _now() + CODE_TTL_SECONDS)
    try:
        _send_email_code(email, code)
    except Exception:
        logger.exception("Failed to send code email")
        # In dev we continue; in prod, raise to force email delivery
        # raise HTTPException(status_code=500, detail="Failed to send email.")
    return {"ok": True}

@app.post(_dual("/verify-code")[0])
@app.post(_dual("/verify-code")[1])
async def verify_code(body: VerifyCodeBody):
    _cleanup_stores()
    email = body.email.lower().strip()
    item = pending_codes.get(email)
    if not item:
        raise HTTPException(status_code=400, detail="No code pending or expired.")
    code_expected, exp = item
    if exp < _now():
        pending_codes.pop(email, None)
        raise HTTPException(status_code=400, detail="Code expired.")
    if body.code.strip() != code_expected:
        raise HTTPException(status_code=401, detail="Invalid code.")
    token = _mint_token()
    issued_tokens[token] = (email, _now() + TOKEN_TTL_SECONDS)
    pending_codes.pop(email, None)
    return {"token": token, "expires_in": TOKEN_TTL_SECONDS}
def setup_logging():
    logger = logging.getLogger("app")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.propagate = False
    return logger

logger = setup_logging()

# =========================
# App
# =========================

@app.on_event("startup")
async def _on_startup():
    logger.info("Startup: app is booting with model key=%s...", "set" if OPENAI_API_KEY else "missing")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info("HTTP %s %s", request.method, request.url.path)
    resp = await call_next(request)
    logger.info("HTTP %s %s -> %d", request.method, request.url.path, resp.status_code)
    return resp

@app.get("/health")
async def health():
    logger.info("Health check hit")
    return {"ok": True}

# =========================
# First-run email code flow (new)
# =========================
from pydantic import BaseModel, EmailStr
from datetime import datetime, timedelta, timezone

# super simple in-memory store; replace with Redis/DB if you want durability
_EMAIL_CODES: dict[str, dict] = {}   # email -> {"code": "123456", "exp": timestamp}
_TOKENS: dict[str, dict] = {}        # token -> {"email": "...", "exp": timestamp}

def _rand_code(n=6) -> str:
    return "".join(random.choices(string.digits, k=n))

def _rand_token(n=40) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=n))

def _now_utc():
    return datetime.now(timezone.utc)

def _send_email_console(email: str, code: str):
    # If SMTP envs not set, just log the code for dev.
    logger.info("[DEV EMAIL] To: %s | Your %s code: %s", email, APP_NAME, code)

def _send_email_smtp(email: str, code: str):
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(f"Your {APP_NAME} verification code is: {code}\nThis code expires in 10 minutes.")
    msg["Subject"] = f"{APP_NAME} — Your Sign-in Code"
    msg["From"] = SENDER_EMAIL
    msg["To"] = email
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

@app.post("/auth/request-code")
async def request_code(body: RequestCodeBody):
    email = body.email.lower().strip()
    code = _rand_code(6)
    exp = _now_utc() + timedelta(minutes=10)
    _EMAIL_CODES[email] = {"code": code, "exp": exp}

    if SMTP_HOST and SMTP_USER and SMTP_PASS:
        try:
            _send_email_smtp(email, code)
        except Exception as e:
            logger.exception("SMTP send failed")
            raise HTTPException(status_code=500, detail=f"Email send failed: {e}")
    else:
        _send_email_console(email, code)

    return {"ok": True, "sent": True}

@app.post("/auth/verify-code")
async def verify_code(body: VerifyCodeBody):
    email = body.email.lower().strip()
    rec = _EMAIL_CODES.get(email)
    if not rec:
        raise HTTPException(status_code=400, detail="Code not found; request a new one.")
    if _now_utc() > rec["exp"]:
        raise HTTPException(status_code=400, detail="Code expired; request a new one.")
    if body.code.strip() != rec["code"]:
        raise HTTPException(status_code=400, detail="Invalid code.")

    # success → mint a simple short-lived token (NOT a JWT; just a bearer string you can store)
    token = _rand_token(40)
    _TOKENS[token] = {"email": email, "exp": _now_utc() + timedelta(days=30)}
    # optional: delete used code
    try: del _EMAIL_CODES[email]
    except: pass
    return {"ok": True, "token": token, "email": email}

# (Optional) tiny helper to validate a token later, if you decide to use it
def validate_token(token: str) -> bool:
    if not token: return False
    rec = _TOKENS.get(token)
    if not rec: return False
    if _now_utc() > rec["exp"]:
        try: del _TOKENS[token]
        except: pass
        return False
    return True

# =========================
# OpenAI proxy (unchanged)
# =========================
def _wrap_if_needed(body: Dict[str, Any]) -> Dict[str, Any]:
    msgs = body.get("messages")
    if isinstance(msgs, list) and msgs:
        return body
    try:
        import json
        user_content = json.dumps(body, ensure_ascii=False)[:12000]
    except Exception:
        user_content = str(body)[:12000]

    wrapped = {
        "model": body.get("model") or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": BACKEND_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }
    return wrapped

@app.post("/api/chat-completions")
async def chat_completions(req: Request):
    global SEARCHES
    try:
        incoming = await req.json()
        body = _wrap_if_needed(incoming)

        msgs = body.get("messages")
        if not isinstance(msgs, list):
            raise HTTPException(status_code=400, detail="Invalid request: 'messages' missing or not a list")

        url = incoming.get("url") or body.get("url")
        if url:
            SEARCHES.append(url)
        else:
            SEARCHES.append("<no-url>")

        if len(SEARCHES) > 200:
            SEARCHES[:] = SEARCHES[-200:]

        model = body.get("model", "unknown-model")
        logger.info("Proxying to OpenAI: model=%s, messages=%d, keys=%s", model, len(msgs), list(body.keys()))
        logger.info("Searcges: model=%s, messages=%d, keys=%s", model, SEARCHES, list(body.keys()))
        timeout = httpx.Timeout(60.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                OPENAI_URL,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )

        logger.info("OpenAI responded: status=%d", r.status_code)

        if r.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail={"upstream_status": r.status_code, "text": r.text[:2000]},
            )

        return r.json()

    except HTTPException:
        raise
    except httpx.HTTPError as e:
        logger.exception("Network error talking to OpenAI")
        raise HTTPException(status_code=502, detail=f"Upstream network error: {e}")
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail=str(e))
