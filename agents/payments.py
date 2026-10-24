"""Payments agent: receives Whop webhooks and marks orders as paid.

Signature check follows the Standard Webhooks spec (https://www.standardwebhooks.com):
  signed content = "{webhook-id}.{webhook-timestamp}.{raw body}"
  signature      = base64(HMAC-SHA256(key, signed content)), sent as "v1,<sig>" in webhook-signature
Whop signs with the webhook secret's raw bytes; secrets in the "whsec_<base64>" form are
also accepted (the base64 part is decoded, as the spec describes).
"""
import base64
import binascii
import hashlib
import hmac
import logging
import time

import config
import db

log = logging.getLogger("payments")


class InvalidSignature(Exception):
    pass


def _candidate_keys(secret: str) -> list[bytes]:
    keys = [secret.encode()]
    raw = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    try:
        keys.append(base64.b64decode(raw, validate=True))
    except (binascii.Error, ValueError):
        pass
    return keys


def verify_signature(body: bytes, headers, secret: str | None = None,
                     now: float | None = None) -> None:
    secret = secret if secret is not None else config.WHOP_WEBHOOK_SECRET
    if not secret:
        raise InvalidSignature("WHOP_WEBHOOK_SECRET is not configured")
    msg_id = headers.get("webhook-id")
    ts = headers.get("webhook-timestamp")
    sig_header = headers.get("webhook-signature")
    if not (msg_id and ts and sig_header):
        raise InvalidSignature("missing webhook-id / webhook-timestamp / webhook-signature")
    try:
        ts_int = int(ts)
    except ValueError:
        raise InvalidSignature("bad timestamp")
    if abs((now or time.time()) - ts_int) > config.WEBHOOK_TOLERANCE_SECONDS:
        raise InvalidSignature("timestamp outside tolerance")

    signed = f"{msg_id}.{ts}.".encode() + body
    received = [part.split(",", 1)[1] for part in sig_header.split() if part.startswith("v1,")]
    for key in _candidate_keys(secret):
        expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
        if any(hmac.compare_digest(expected, sig) for sig in received):
            return
    raise InvalidSignature("no matching signature")


def _find_email(obj) -> str | None:
    """Whop payloads have shifted shape across API versions; look in the usual places, then anywhere."""
    data = obj.get("data", {}) if isinstance(obj, dict) else {}
    for path in (("user", "email"), ("email",), ("customer", "email"), ("member", "email"),
                 ("membership", "user", "email"), ("billing_address", "email")):
        cur = data
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if isinstance(cur, str) and "@" in cur:
            return cur
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if "email" in k.lower() and isinstance(v, str) and "@" in v:
                    return v
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def handle_event(event_id: str, payload: dict) -> dict:
    """Process a verified Whop event. Returns a small status dict (also used as the HTTP response)."""
    event_type = payload.get("type") or payload.get("action") or payload.get("event")
    if event_type != "payment.succeeded":
        return {"ok": True, "ignored": event_type}
    if not db.mark_webhook_seen(event_id):
        return {"ok": True, "duplicate": True}

    data = payload.get("data") or {}
    email = _find_email(payload)
    if not email:
        log.warning("payment.succeeded %s has no buyer email", data.get("id"))
        return {"ok": True, "matched": False, "reason": "no buyer email in payload"}

    order = db.latest_pending_order_for(email)
    if not order:
        log.warning("No pending order for %s (payment %s)", email, data.get("id"))
        return {"ok": True, "matched": False, "reason": "no pending order for buyer"}

    db.update_order(order["id"], status="paid", payment_id=data.get("id"), paid_at=db.now())
    log.info("Order %s paid (payment %s)", order["id"], data.get("id"))
    return {"ok": True, "matched": True, "order_id": order["id"]}
