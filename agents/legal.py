"""Legal agent: blocks offensive or unsafe questions before any human panel sees them.

Runs twice: at intake (so bad orders never become payable) and again right before Ops
launches a study. Uses Claude as the main reviewer, with a keyword blocklist as a
fallback when the LLM is unavailable.
"""
import logging
import re

from clients import llm

log = logging.getLogger("legal")

# Fallback only. Kept short and unambiguous to avoid blocking normal questions.
_BLOCKLIST = [
    r"\bbomb[- ]?making\b", r"\bhow to (kill|murder|poison)\b", r"\bsuicide method", r"\bself[- ]harm\b",
    r"\bchild (porn|sexual)", r"\bcsam\b", r"\bnude(s)?\b", r"\bporn\b", r"\bterroris[mt]\b",
    r"\bgenocide\b", r"\bnazi\b", r"\bwhite power\b", r"\bdox(x)?(ing)?\b", r"\bmeth recipe\b",
    r"\bbuy (a )?gun without\b", r"\bmake (a )?weapon\b",
]

POLICY = """You are the Legal agent of a company that sends a customer's question and answer options
to 10 members of the general public, who pick the best option and give a one-line reason.
REJECT the order if the question or any option:
- is hateful, harassing, sexually explicit, or demeaning toward people or groups
- seeks help with violence, weapons, self-harm, crime, drugs, or other dangerous activity
- contains personal information about a private individual (names with contact details, addresses, etc.)
- would expose panelists to graphic or disturbing content
- is spam, a scam, or an attempt to recruit/solicit the panelists
APPROVE ordinary decisions (product names, designs described in words, travel, career choices, copy, etc.).
Return {"approved": true|false, "reason": "<one short sentence>"}."""


def review(question: str, options: list[str]) -> tuple[bool, str]:
    text = question + "\n" + "\n".join(options)
    for pattern in _BLOCKLIST:
        if re.search(pattern, text, re.I):
            return False, "Contains unsafe or offensive content."
    try:
        verdict = llm.ask_json(POLICY, "Question: " + question + "\nOptions:\n" +
                               "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options)), max_tokens=200)
        approved = bool(verdict.get("approved"))
        return approved, str(verdict.get("reason") or ("Approved." if approved else "Rejected by policy."))
    except llm.LLMUnavailable as e:
        log.warning("LLM unavailable, keyword check only: %s", e)
        return True, "Passed keyword check (LLM review unavailable)."
