"""Intake agent: accepts new orders from the website and stores them as "pending".

Legal reviews the order first; rejected orders are stored as "rejected" so the
customer gets an immediate answer instead of paying for something we won't run.
"""
import re

from pydantic import BaseModel, Field, field_validator

import db
from agents import ceo, legal

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OrderIn(BaseModel):
    email: str = Field(max_length=254)
    question: str = Field(min_length=3, max_length=500)
    options: list[str] = Field(min_length=2, max_length=4)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v

    @field_validator("question")
    @classmethod
    def _question(cls, v: str) -> str:
        return v.strip()

    @field_validator("options")
    @classmethod
    def _options(cls, v: list[str]) -> list[str]:
        cleaned = [o.strip() for o in v if o and o.strip()]
        if len(cleaned) < 2:
            raise ValueError("at least two non-empty options are required")
        if any(len(o) > 200 for o in cleaned):
            raise ValueError("each option must be 200 characters or fewer")
        return cleaned


def create_order(order: OrderIn) -> dict:
    approved, reason = legal.review(order.question, order.options)
    price = ceo.current_price()
    if not approved:
        saved = db.create_order(order.email, order.question, order.options, "rejected", price, reason)
        ceo.reject_order(saved, reason, stage="intake")
        return saved
    saved = db.create_order(order.email, order.question, order.options, "pending", price)
    ceo.accept_order(saved)
    return saved
