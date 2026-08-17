"""Deterministic contracts for the support-reply example suite.

Real checks for a real prompt: each one encodes a hard rule from
support-reply.md, so a fail golden trips at least one and every pass golden
clears them all. Reasons carry the machine-greppable [tag] prefix from the
failure taxonomy (same convention as src/contracts_lib.py)."""


def _ok(reason):
    return {"pass": True, "score": 1.0, "reason": reason}


def _fail(tag, reason):
    return {"pass": False, "score": 0.0, "reason": f"[{tag}] {reason}"}


def acknowledges_customer(output, context):
    """Rule 1: the reply opens by acknowledging the customer's situation —
    an apology or thanks in the first couple of sentences, never a cold
    start or a canned 'valued customer' opener."""
    text = str(output).lower()
    head = text[:300]
    if "valued customer" in head:
        return _fail("tone", "canned 'valued customer' opener instead of acknowledging the actual problem")
    if any(marker in head for marker in ("sorry", "thanks for", "thank you", "i'm glad")):
        return _ok("opens by acknowledging the customer")
    return _fail("tone", "no acknowledgment (sorry/thanks) in the opening lines")


def has_next_step(output, context):
    """Rule 3: one concrete next step — something committed or actionable,
    not a reply that just describes the problem back."""
    text = str(output).lower()
    markers = ("i've ", "i have ", "we'll ", "we will ", "you can ", "please ",
               "your replacement", "your refund", "expect ", "within ")
    if any(m in text for m in markers):
        return _ok("a concrete next step is present")
    return _fail("missing-content", "no concrete next step (nothing committed, nothing asked of the customer)")


def no_internal_leak(output, context):
    """Hard rule: the customer never sees internal systems or an AI
    disclosure — the reply reads as a person's message."""
    text = str(output).lower()
    for phrase in ("as an ai", "language model", "system prompt", "internal policy",
                   "our ticketing", "ticket queue", "i cannot access"):
        if phrase in text:
            return _fail("instruction-miss", f'internal/AI leak: "{phrase}"')
    return _ok("no internal or AI references")


def no_unfilled_template(output, context):
    """A reply with unfilled placeholders was never customized — the exact
    failure a template-happy prompt produces under pressure."""
    text = str(output)
    for marker in ("[insert", "[INSERT", "[name]", "[Name]", "{{", "[order number]", "XXXX"):
        if marker in text:
            return _fail("format", f'unfilled template placeholder: "{marker}"')
    return _ok("no unfilled placeholders")
