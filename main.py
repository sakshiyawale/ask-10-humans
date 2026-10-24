"""Ask 10 Humans: Zero-Human Company. FastAPI entry point."""
import asyncio
import html
import json
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from fastapi import BackgroundTasks, FastAPI, Form, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

import config
import db
from agents import analyst, ceo, intake, ops, payments

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")
esc = html.escape


async def monitor_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(ops.tick)
        except Exception:  # noqa: BLE001 - never let the loop die
            log.exception("monitor tick failed")
        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    task = asyncio.create_task(monitor_loop()) if config.MONITOR_INTERVAL_SECONDS > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Ask 10 Humans: Zero-Human Company", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"name": "Ask 10 Humans", "whop_business_id": config.WHOP_BUSINESS_ID,
            "price_usd": ceo.current_price(), "docs": "/docs"}


@app.get("/price")
def price():
    return {"price_usd": ceo.current_price()}


# ---------- Intake ----------

@app.post("/orders", status_code=201)
def create_order(order: intake.OrderIn):
    saved = intake.create_order(order)
    body = {"id": saved["id"], "status": saved["status"], "price_usd": saved["price_usd"],
            "report_url": f"{config.PUBLIC_BASE_URL}/orders/{saved['id']}"}
    if saved["status"] == "rejected":
        body["reason"] = saved["status_reason"]
        return JSONResponse(body, status_code=422)
    return body


def _public_order(o: dict) -> dict:
    # Email and payment details are deliberately left out: anyone with the id can view this.
    return {"id": o["id"], "question": o["question"], "options": o["options"], "status": o["status"],
            "status_reason": o["status_reason"], "created_at": o["created_at"],
            "votes_received": len(db.votes_for(o["id"])), "panel_size": config.PANEL_SIZE,
            "report": o["report"]}


@app.get("/orders/{order_id}")
def get_order(order_id: str, request: Request, format: str | None = None):
    o = db.get_order(order_id)
    if not o:
        raise HTTPException(404, "order not found")
    data = _public_order(o)
    wants_html = format == "html" or (format != "json" and "text/html" in request.headers.get("accept", ""))
    if not wants_html:
        return data
    if o["report"]:
        inner = analyst.report_html(o, o["report"])
    else:
        inner = (f"<h2>{esc(o['question'])}</h2><p>Status: <b>{esc(o['status'])}</b></p>"
                 f"<p>{data['votes_received']} of {config.PANEL_SIZE} votes in so far.</p>")
    return HTMLResponse(_page("Your Ask 10 Humans report", inner))


# ---------- Payments ----------

@app.post("/api/whop-webhook")
async def whop_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    try:
        payments.verify_signature(body, request.headers)
    except payments.InvalidSignature as e:
        log.warning("Rejected webhook: %s", e)
        raise HTTPException(401, "invalid signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    result = payments.handle_event(request.headers["webhook-id"], payload)
    if result.get("matched"):
        background.add_task(ops.launch_study, result["order_id"])
    return result


# ---------- Panelist voting page (the Terac task_url) ----------

def _page(title: str, inner: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 16px;line-height:1.5;color:#111;background:#fff}}
label.opt{{display:block;border:1px solid #ccc;border-radius:8px;padding:12px;margin:8px 0;cursor:pointer}}
input[type=text]{{width:100%;padding:10px;border:1px solid #ccc;border-radius:8px;box-sizing:border-box}}
button{{margin-top:16px;padding:12px 20px;border:0;border-radius:8px;background:#111;color:#fff;font-size:1rem}}
table{{border-collapse:collapse}} td{{border-bottom:1px solid #eee}}</style></head><body>{inner}</body></html>"""


@app.get("/vote/{order_id}", response_class=HTMLResponse)
def vote_form(order_id: str, request: Request):
    o = db.get_order(order_id)
    if not o:
        raise HTTPException(404, "study not found")
    sub = request.query_params.get("teracSubmissionId") or request.query_params.get("submissionId")
    if o["status"] != "in_field":
        return _page("Study closed", "<h2>This study is closed.</h2><p>Thanks for your interest!</p>")
    if not sub and not config.ALLOW_ANON_VOTES:
        return _page("Preview", "<h2>Preview</h2><p>Please open this study from Terac to take part.</p>")
    hidden = "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
                     for k, v in request.query_params.items())
    opts = "".join(f'<label class="opt"><input type="radio" name="option_index" value="{i}" required> {esc(opt)}</label>'
                   for i, opt in enumerate(o["options"]))
    inner = (f"<h2>{esc(o['question'])}</h2><p>Pick the option you think is best:</p>"
             f'<form method="post">{hidden}{opts}<p><label>In one sentence, why?<br>'
             f'<input type="text" name="reason" minlength="3" maxlength="280" required></label></p>'
             f"<button type=submit>Submit</button></form>")
    return _page("Pick the best option", inner)


@app.post("/vote/{order_id}")
async def vote_submit(order_id: str, request: Request, option_index: int = Form(...),
                      reason: str = Form(..., min_length=3, max_length=280)):
    o = db.get_order(order_id)
    if not o:
        raise HTTPException(404, "study not found")
    if o["status"] != "in_field":
        return HTMLResponse(_page("Study closed", "<h2>This study is closed.</h2>"), status_code=409)
    if not 0 <= option_index < len(o["options"]):
        raise HTTPException(400, "invalid option")
    form = await request.form()
    passthrough = {k: v for k, v in form.items() if k not in ("option_index", "reason")}
    sub = passthrough.get("teracSubmissionId") or passthrough.get("submissionId")
    if not sub:
        if not config.ALLOW_ANON_VOTES:
            raise HTTPException(400, "missing Terac submission id")
        sub = "anon-" + db.now()
    db.add_vote(order_id, str(sub), option_index, reason.strip())

    if passthrough.get("teracSubmissionId") or passthrough.get("submissionId"):
        params = {k: passthrough[k] for k in ("submissionId", "teracSubmissionId", "taskId") if k in passthrough}
        params.setdefault("submissionId", sub)
        params.setdefault("teracSubmissionId", sub)
        return RedirectResponse(f"{config.TERAC_CALLBACK_URL}?{urlencode(params)}", status_code=303)
    return HTMLResponse(_page("Thanks", "<h2>Thanks! Your vote was recorded.</h2>"))


# ---------- CEO ----------

class PriceIn(BaseModel):
    price_usd: float = Field(gt=0, le=1000)
    reason: str = Field(min_length=3, max_length=300)


def _require_admin(token: str | None) -> None:
    if not config.ADMIN_TOKEN or token != config.ADMIN_TOKEN:
        raise HTTPException(401, "admin token required")


@app.post("/ceo/price")
def change_price(body: PriceIn, x_admin_token: str | None = Header(default=None)):
    _require_admin(x_admin_token)
    return ceo.set_price(body.price_usd, body.reason)


@app.get("/ceo/decisions")
def decisions(limit: int = 100, x_admin_token: str | None = Header(default=None)):
    _require_admin(x_admin_token)
    return ceo.recent(min(limit, 1000))
