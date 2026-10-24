"""Analyst agent: tallies the votes, has Claude summarize the reasons, and emails the buyer.

The winner and vote split are computed in code (never by the LLM) so the numbers are exact.
Claude only picks the three most useful reasons and writes a one-paragraph summary.
"""
import html
import logging
from collections import Counter

import config
import db
from clients import llm, mailer

log = logging.getLogger("analyst")

esc = html.escape


def tally(order: dict, votes: list[dict]) -> dict:
    counts = Counter(v["option_index"] for v in votes)
    split = [{"option": opt, "votes": counts.get(i, 0)} for i, opt in enumerate(order["options"])]
    top = max(split, key=lambda s: s["votes"]) if votes else None
    tied = [s["option"] for s in split if top and s["votes"] == top["votes"]]
    return {
        "total_votes": len(votes),
        "vote_split": split,
        "winner": tied[0] if len(tied) == 1 else None,
        "tied": tied if len(tied) > 1 else [],
    }


def summarize(order: dict, votes: list[dict], t: dict) -> dict:
    reasons = [f"- [{order['options'][v['option_index']]}] {v['reason']}" for v in votes]
    split_text = ", ".join("%s: %d" % (s["option"], s["votes"]) for s in t["vote_split"])
    winner_text = t["winner"] or "tie between " + " and ".join(t["tied"])
    try:
        out = llm.ask_json(
            "You are the Analyst at Ask 10 Humans. You turn a small public vote into a crisp report.",
            f"Question: {order['question']}\n"
            f"Vote split: {split_text}\n"
            f"Winner: {winner_text}\n"
            f"Reasons given (option in brackets):\n" + "\n".join(reasons) + "\n\n"
            'Return {"top_reasons": [three short reasons, most persuasive first, each prefixed with the '
            'option it supports], "summary": "two sentences for the buyer"}. Quote or lightly paraphrase '
            "panelists; do not invent reasons.",
        )
        top = [str(r) for r in out.get("top_reasons", [])][:3]
        summary = str(out.get("summary", ""))
    except llm.LLMUnavailable as e:
        log.warning("LLM unavailable, using simple summary: %s", e)
        top, summary = [], ""
    if not top:
        win_idx = order["options"].index(t["winner"]) if t["winner"] else None
        preferred = [v for v in votes if v["option_index"] == win_idx] or votes
        top = [f"[{order['options'][v['option_index']]}] {v['reason']}" for v in preferred[:3]]
    if not summary:
        summary = (f"{t['winner']} won with {max(s['votes'] for s in t['vote_split'])} of {t['total_votes']} votes."
                   if t["winner"] else f"It's a tie between {' and '.join(t['tied'])}.")
    return {"top_reasons": top, "summary": summary}


def build_report(order: dict, partial: bool = False) -> dict:
    votes = db.votes_for(order["id"])
    t = tally(order, votes)
    return {**t, **summarize(order, votes, t), "partial": partial, "panel_size": config.PANEL_SIZE,
            "generated_at": db.now()}


def report_html(order: dict, report: dict) -> str:
    rows = "".join(f"<tr><td>{esc(s['option'])}</td><td style='text-align:right'><b>{s['votes']}</b></td></tr>"
                   for s in report["vote_split"])
    reasons = "".join(f"<li>{esc(r)}</li>" for r in report["top_reasons"])
    headline = (f"Winner: {esc(report['winner'])}" if report["winner"]
                else f"Tie: {esc(' / '.join(report['tied']))}")
    note = (f"<p><i>Only {report['total_votes']} of {report['panel_size']} people answered in time, "
            f"so this is a partial result.</i></p>" if report["partial"] else "")
    link = f"{config.PUBLIC_BASE_URL}/orders/{order['id']}"
    return (f"<h2>{esc(order['question'])}</h2><h3>{headline}</h3>{note}"
            f"<table cellpadding='6'>{rows}</table><h4>Top reasons</h4><ol>{reasons}</ol>"
            f"<p>{esc(report['summary'])}</p><p><a href='{link}'>View your report online</a></p>"
            f"<p style='color:#888'>Ask 10 Humans, a company run entirely by AI agents.</p>")


def deliver(order_id: str, partial: bool = False) -> None:
    order = db.get_order(order_id)
    if not order or order["status"] != "in_field":
        return
    report = build_report(order, partial)
    db.update_order(order_id, status="completed", report=report, completed_at=db.now())
    try:
        mailer.send(order["email"], f"Your result: {order['question'][:60]}", report_html(order, report))
    except Exception as e:  # noqa: BLE001 - the report is still visible at /orders/{id}
        log.error("Report email failed for %s: %s", order_id, e)
        db.update_order(order_id, status_reason=f"Email failed: {str(e)[:200]}")
