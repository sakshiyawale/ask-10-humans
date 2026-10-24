"""Ops agent: turns a paid order into a live Terac study, then watches it.

Flow for each paid order:
  1. Legal re-checks the question (last gate before real people see it).
  2. Create a draft opportunity on Terac: general population (US adults 18+),
     PANEL_SIZE people, one "interview" task whose task_url is our own /vote page.
  3. Launch it (TERAC_AUTO_LAUNCH). The customer paying is the go-ahead.
  4. Panelists vote on /vote/{order_id}; we redirect them to Terac's completion
     callback, which marks their submission complete and pays them (auto_approve).

The monitor loop (main.py) calls `tick()` every minute: once all votes are in it
hands the order to Analyst; past the deadline it asks the CEO to refund or deliver
partial results.
"""
import logging
from datetime import datetime, timedelta, timezone

import config
import db
from agents import analyst, ceo, legal
from clients import mailer, whop
from clients.terac import TeracError, TeracMCP, find

log = logging.getLogger("ops")


def _project_id(terac: TeracMCP) -> str:
    pid = config.TERAC_PROJECT_ID or db.get_setting("terac_project_id")
    if pid:
        return pid
    res = terac.call("terac_create_project", {"name": "Ask 10 Humans"})
    pid = find(res, "id") or find(res, "project_id")
    if not pid:
        raise TeracError(f"could not read project id from {res}")
    db.set_setting("terac_project_id", pid)
    return pid


def opportunity_spec(order: dict, project_id: str) -> dict:
    n = config.PANEL_SIZE
    return {
        "title": "Quick opinion: pick the best option (about 2 minutes)",
        "internal_title": f"Ask 10 Humans {order['id']}",
        "description": ("Read one short question, pick the option you think is best, and write one "
                        "sentence explaining why. Takes about 2 minutes."),
        "project_id": project_id,
        "num_participants": n,
        "business_type": "b2c",
        "unrestricted_audience": True,  # general population: US adults 18+
        "customer_screening_review": "auto_invite",
        "screening_questions": [{
            "key": "english",
            "text": "Can you comfortably read a short question and write a one-sentence answer in English?",
            "pick": "boolean",
            "answers": [{"text": "Yes", "qualify_logic": "must"},
                        {"text": "No", "qualify_logic": "reject"}],
        }],
        "tasks": [{
            "sequence": 1,
            "task_type": "interview",
            "review_type": "auto_approve",
            "task_url": f"{config.PUBLIC_BASE_URL}/vote/{order['id']}",
            "title": "Pick the best option",
            "description": ("Open the link, read the question, choose the option you think is best, and "
                            "write one sentence on why. Submitting returns you to Terac automatically."),
            "duration_minutes": config.TASK_MINUTES,
        }],
    }


def _study_cost(opportunity: dict) -> float | None:
    for key in ("total_cost", "estimated_total", "total", "budget", "estimated_cost"):
        val = find(opportunity.get("pricing", opportunity), key)
        if isinstance(val, (int, float)):
            return float(val)
    cpi = find(opportunity, "cost_per_participant") or find(opportunity, "costPerParticipant")
    return float(cpi) * config.PANEL_SIZE if isinstance(cpi, (int, float)) else None


def launch_study(order_id: str, terac: TeracMCP | None = None) -> None:
    if not db.claim(order_id, "paid", "launching"):
        return  # not paid, or another worker is already launching it
    order = db.get_order(order_id)

    approved, reason = legal.review(order["question"], order["options"])
    if not approved:
        db.update_order(order_id, status="rejected", status_reason=reason)
        ceo.reject_order(order, reason, stage="pre_launch")
        refund(order, f"Rejected by Legal before launch: {reason}")
        return

    terac = terac or TeracMCP()
    try:
        opp_id = order.get("terac_opportunity_id")  # set if an earlier attempt created the draft
        if not opp_id:
            draft = terac.call("terac_create_opportunity", opportunity_spec(order, _project_id(terac)))
            opp_id = find(draft, "id") or find(draft, "opportunityId") or find(draft, "opportunity_id")
            if not opp_id:
                raise TeracError(f"could not read opportunity id from {str(draft)[:300]}")
            db.update_order(order_id, terac_opportunity_id=opp_id)
            ceo.review_unit_economics(order_id, _study_cost(draft))

        if not config.TERAC_AUTO_LAUNCH:
            db.update_order(order_id, status="awaiting_launch",
                            status_reason="Draft created; TERAC_AUTO_LAUNCH is off")
            return
        terac.call("terac_launch_draft_opportunity", {"opportunityId": opp_id})
        db.update_order(order_id, status="in_field", launched_at=db.now())
        log.info("Order %s launched as Terac opportunity %s", order_id, opp_id)
    except TeracError as e:
        log.error("Terac launch failed for %s: %s", order_id, e)
        # Stay "paid": the monitor retries, and the CEO refunds at the deadline if it never launches.
        db.update_order(order_id, status="paid", status_reason=f"Terac error: {str(e)[:300]}")


def refund(order: dict, reason: str) -> None:
    try:
        whop.refund_payment(order.get("payment_id"))
        refunded = True
    except Exception as e:  # noqa: BLE001 - we still want the decision recorded
        log.error("Refund failed for %s: %s", order["id"], e)
        refunded = False
    ceo.log("refund", reason, order_id=order["id"], payment_id=order.get("payment_id"),
            refunded=refunded, amount_usd=order.get("price_usd"))
    db.update_order(order["id"], status="refunded" if refunded else "refund_failed", status_reason=reason)
    try:
        mailer.send(order["email"], "Your Ask 10 Humans order was refunded",
                    f"<p>We couldn't deliver results for “{analyst.esc(order['question'])}”, so we've issued "
                    f"a full refund.</p><p>Reason: {analyst.esc(reason)}</p>")
    except Exception as e:  # noqa: BLE001
        log.warning("Refund email failed for %s: %s", order["id"], e)


def _stop(order: dict, why: str) -> None:
    if not order.get("terac_opportunity_id"):
        return
    try:
        TeracMCP().call("terac_stop_opportunity", {"opportunityId": order["terac_opportunity_id"], "reason": why})
    except TeracError as e:
        log.warning("Could not stop opportunity %s: %s", order["terac_opportunity_id"], e)


def _age_minutes(iso: str | None) -> float:
    if not iso:
        return 0.0
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)) / timedelta(minutes=1)


def tick() -> None:
    """One pass of the monitor loop."""
    for order in db.orders_with_status("paid"):
        if _age_minutes(order["paid_at"]) >= config.REFUND_AFTER_MINUTES:
            refund(order, f"Study could not be launched within {config.REFUND_AFTER_MINUTES} minutes")
        else:
            launch_study(order["id"])  # first attempt or retry after a Terac error

    for order in db.orders_with_status("launching", "awaiting_launch"):
        # "launching" left behind by a crash, or a draft nobody launched in time.
        if _age_minutes(order["paid_at"]) >= config.REFUND_AFTER_MINUTES + 5:
            refund(order, f"Study was not live within {config.REFUND_AFTER_MINUTES} minutes")

    for order in db.orders_with_status("in_field"):
        votes = db.votes_for(order["id"])
        if len(votes) >= config.PANEL_SIZE:
            analyst.deliver(order["id"])
        elif _age_minutes(order["paid_at"]) >= config.REFUND_AFTER_MINUTES:
            why = f"No complete results within {config.REFUND_AFTER_MINUTES} minutes"
            _stop(order, why)
            if votes:
                ceo.log("deliver_partial", why, order_id=order["id"], votes=len(votes))
                analyst.deliver(order["id"], partial=True)
            else:
                refund(order, f"No results within {config.REFUND_AFTER_MINUTES} minutes")
