"""End-to-end tests with Terac, Whop refunds, Resend and the LLM stubbed out."""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SECRET = "ws_test_secret_123"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # Set before importing config so the local .env can't leak real keys into tests.
    for k, v in {
        "DATABASE_PATH": str(tmp_path / "t.db"), "DECISIONS_LOG_PATH": str(tmp_path / "decisions.log"),
        "WHOP_WEBHOOK_SECRET": SECRET, "ANTHROPIC_API_KEY": "", "RESEND_API_KEY": "", "WHOP_API_KEY": "",
        "TERAC_API_KEY": "", "MONITOR_INTERVAL_SECONDS": "0", "PUBLIC_BASE_URL": "https://api.example.com",
        "ADMIN_TOKEN": "adm", "TERAC_PROJECT_ID": "", "ALLOW_ANON_VOTES": "false",
    }.items():
        monkeypatch.setenv(k, v)
    for mod in [m for m in sys.modules if m in ("config", "db", "main") or m.startswith(("agents", "clients"))]:
        del sys.modules[mod]
    import main
    from fastapi.testclient import TestClient

    sent, refunds = [], []
    monkeypatch.setattr(main.ops.mailer, "send", lambda to, subj, html: sent.append((to, subj)) or {})
    monkeypatch.setattr(main.ops.whop, "refund_payment", lambda pid: refunds.append(pid) or {})
    with TestClient(main.app) as client:
        yield client, main, sent, refunds, tmp_path


class FakeTerac:
    def __init__(self):
        self.calls = []

    def call(self, tool, args=None):
        self.calls.append((tool, args))
        if tool == "terac_create_project":
            return {"id": "proj_1"}
        if tool == "terac_create_opportunity":
            return {"id": "opp_1", "status": "draft", "pricing": {"total_cost": 30.0}}
        return {"id": "opp_1", "status": "active"}


def sign(body: bytes, msg_id="msg_1", ts=None, key=SECRET.encode()):
    ts = str(int(ts or time.time()))
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"webhook-id": msg_id, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}",
            "content-type": "application/json"}


def new_order(client, email="Buyer@Example.com"):
    r = client.post("/orders", json={"email": email, "question": "Which name for my bakery?",
                                     "options": ["Rise & Shine", "Crumb Club", "Loaf Story"]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def pay(client, email="buyer@example.com", msg_id="msg_1"):
    body = json.dumps({"type": "payment.succeeded",
                       "data": {"id": "pay_123", "user": {"email": email}}}).encode()
    return client.post("/api/whop-webhook", content=body, headers=sign(body, msg_id))


def test_health(env):
    client, *_ = env
    assert client.get("/health").json() == {"status": "ok"}


def test_intake_validation_and_cors(env):
    client, *_ = env
    assert client.post("/orders", json={"email": "x", "question": "q?", "options": ["a"]}).status_code == 422
    r = client.options("/orders", headers={"Origin": "https://site.lovable.app",
                                           "Access-Control-Request-Method": "POST"})
    assert r.headers["access-control-allow-origin"] in ("*", "https://site.lovable.app")


def test_legal_rejects_unsafe(env):
    client, main, *_ = env
    r = client.post("/orders", json={"email": "a@b.co", "question": "Best bomb-making guide?",
                                     "options": ["A", "B"]})
    assert r.status_code == 422 and r.json()["status"] == "rejected"
    assert main.ceo.recent()[-1]["decision"] == "order_rejected"


def test_webhook_signature_required(env):
    client, *_ = env
    body = b'{"type":"payment.succeeded","data":{}}'
    bad = sign(body, key=b"wrong")
    assert client.post("/api/whop-webhook", content=body, headers=bad).status_code == 401
    old = sign(body, ts=time.time() - 3600)
    assert client.post("/api/whop-webhook", content=body, headers=old).status_code == 401


def test_whsec_secret_format(env):
    _, main, *_ = env
    key = os.urandom(24)
    secret = "whsec_" + base64.b64encode(key).decode()
    body = b"{}"
    main.payments.verify_signature(body, sign(body, key=key), secret=secret)


def test_full_flow(env, monkeypatch):
    client, main, sent, refunds, tmp = env
    oid = new_order(client)
    fake = FakeTerac()
    monkeypatch.setattr(main.ops, "TeracMCP", lambda: fake)

    r = pay(client)
    assert r.json()["matched"] and r.json()["order_id"] == oid
    assert pay(client).json().get("duplicate")  # same webhook id is processed once

    order = client.get(f"/orders/{oid}").json()
    assert order["status"] == "in_field" and "email" not in order
    tools = [c[0] for c in fake.calls]
    assert tools == ["terac_create_project", "terac_create_opportunity", "terac_launch_draft_opportunity"]
    spec = fake.calls[1][1]
    assert spec["num_participants"] == 10 and spec["unrestricted_audience"] is True
    assert spec["tasks"][0]["task_url"] == f"https://api.example.com/vote/{oid}"

    assert "Preview" in client.get(f"/vote/{oid}").text  # no submission id -> no form
    page = client.get(f"/vote/{oid}?teracSubmissionId=s0&taskId=t1")
    assert "Crumb Club" in page.text
    for i in range(10):
        r = client.post(f"/vote/{oid}", data={"option_index": 1 if i < 7 else 0, "reason": f"reason {i}",
                                              "teracSubmissionId": f"s{i}", "taskId": "t1"},
                        follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("https://terac.com/api/external/callback?")

    main.ops.tick()
    order = client.get(f"/orders/{oid}").json()
    rep = order["report"]
    assert order["status"] == "completed"
    assert rep["winner"] == "Crumb Club" and rep["vote_split"][1]["votes"] == 7 and len(rep["top_reasons"]) == 3
    assert sent and sent[-1][0] == "buyer@example.com"
    assert "Crumb Club" in client.get(f"/orders/{oid}?format=html").text


def test_refund_after_deadline(env, monkeypatch):
    client, main, sent, refunds, tmp = env
    oid = new_order(client)
    monkeypatch.setattr(main.ops, "TeracMCP", lambda: FakeTerac())
    pay(client)
    main.db.update_order(oid, paid_at="2000-01-01T00:00:00+00:00")
    main.ops.tick()
    assert client.get(f"/orders/{oid}").json()["status"] == "refunded"
    assert refunds == ["pay_123"]
    decisions = [d["decision"] for d in main.ceo.recent()]
    assert decisions == ["order_accepted", "price_review", "refund"]


def test_ceo_price_change(env):
    client, main, *_ = env
    assert client.post("/ceo/price", json={"price_usd": 12, "reason": "test"}).status_code == 401
    r = client.post("/ceo/price", json={"price_usd": 12, "reason": "Demand is high"},
                    headers={"x-admin-token": "adm"})
    assert r.json()["new_price_usd"] == 12
    assert client.get("/price").json()["price_usd"] == 12
