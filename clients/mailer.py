"""Send email through Resend."""
import httpx

import config


def send(to: str, subject: str, html: str) -> dict:
    if not config.RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY is not set")
    r = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
        json={"from": config.RESEND_FROM, "to": [to], "subject": subject, "html": html},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Resend failed ({r.status_code}): {r.text[:300]}")
    return r.json()
