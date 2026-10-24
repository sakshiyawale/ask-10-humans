"""Whop REST calls (refunds)."""
import httpx

import config


def refund_payment(payment_id: str) -> dict:
    if not config.WHOP_API_KEY:
        raise RuntimeError("WHOP_API_KEY is not set")
    if not payment_id:
        raise RuntimeError("order has no Whop payment id")
    r = httpx.post(
        f"{config.WHOP_API_BASE}/payments/{payment_id}/refund",
        headers={"Authorization": f"Bearer {config.WHOP_API_KEY}"},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Whop refund failed ({r.status_code}): {r.text[:300]}")
    return r.json() if r.content else {}
