"""CEO agent: makes company-level decisions and writes every one to decisions.log.

Decisions it owns:
- order_accepted / order_rejected   (after Legal's review)
- refund                            (no results within REFUND_AFTER_MINUTES)
- deliver_partial                   (deadline hit with some, but not all, votes in)
- price_change                      (manual via /ceo/price, or automatic when Terac costs exceed price)
"""
import json
import math
import threading

import config
import db

_lock = threading.Lock()


def log(decision: str, reason: str, **details) -> dict:
    entry = {"ts": db.now(), "agent": "ceo", "decision": decision, "reason": reason, **details}
    with _lock, open(config.DECISIONS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def recent(limit: int = 100) -> list[dict]:
    try:
        with open(config.DECISIONS_LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


# ---------- pricing ----------

def current_price() -> float:
    return float(db.get_setting("price_usd", str(config.PRICE_USD)))


def set_price(new_price: float, reason: str) -> dict:
    old = current_price()
    db.set_setting("price_usd", f"{new_price:.2f}")
    return log("price_change", reason, old_price_usd=old, new_price_usd=round(new_price, 2))


def review_unit_economics(order_id: str, study_cost_usd: float | None) -> None:
    """Called after Ops prices a study. Raises the price if we'd lose money (when allowed)."""
    if study_cost_usd is None:
        return
    price = current_price()
    if study_cost_usd * (1 + config.TARGET_MARGIN) <= price:
        return
    suggested = float(math.ceil(study_cost_usd * (1 + config.TARGET_MARGIN)))
    if config.CEO_AUTO_PRICING:
        set_price(suggested, f"Terac cost ${study_cost_usd:.2f} for order {order_id} leaves less than "
                             f"{config.TARGET_MARGIN:.0%} margin at ${price:.2f}")
    else:
        log("price_review", "Study cost exceeds target margin; auto-pricing is off, price unchanged",
            order_id=order_id, study_cost_usd=study_cost_usd, price_usd=price, suggested_price_usd=suggested)


# ---------- order decisions ----------

def accept_order(order: dict) -> None:
    log("order_accepted", "Passed Legal review", order_id=order["id"], price_usd=order["price_usd"])


def reject_order(order: dict, reason: str, stage: str) -> None:
    log("order_rejected", reason, order_id=order["id"], stage=stage)
