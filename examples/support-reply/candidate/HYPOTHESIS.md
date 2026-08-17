# Candidate hypothesis (write this BEFORE the compare)

**Observation** (from the graded baseline): replies to angry and repeat-contact
messages score lowest on `empathy_tone` — they jump straight to process
("I've filed a trace") without first naming what the customer is feeling, and
occasionally reuse the same stock opener across cells.

**One deliberate change**: rule 1 now requires mirroring the customer's own
words for the problem and, on repeat contacts, explicitly owning the failure
("you shouldn't have had to write three times"). Nothing else was touched.

**Prediction**: `empathy_tone` mean improves by ≥ 0.5 on the angry/late cells;
`resolution`, `accuracy`, and `scope_safety` stay within ±0.25. If any other
dimension drops more than that, the change is a regression regardless of how
much warmer the replies read.

**Why hypothesis-first matters**: the verdict machinery is blinded and
position-swapped, but a hypothesis written *after* seeing scores degrades
into curve-fitting. State the prediction, then run:

    cp examples/support-reply/candidate/support-reply.md prompts/candidates/
    uv run prompt-eval compare --suite support-reply
    uv run prompt-eval verdict --target empathy_tone
