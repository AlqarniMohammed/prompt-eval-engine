"""Local read-only dashboard (`prompt-eval dashboard`).

Complements `promptfoo view`, never re-implements it: promptfoo's viewer owns
per-case output browsing; this dashboard owns what the engine owns — spend
and governance, the pipeline state machine, judge evidence, and history
trends. Strictly two layers:

    data.py           pure aggregation: Paths in, JSON-able dicts out,
                      tolerant of partial/missing/torn files everywhere
    server.py         thin stdlib HTTP route table, 127.0.0.1 only, GET only
    pipeline_docs.py  STAGE_DOCS — the single source of stage explainer copy
    index.html        one self-contained page (inline CSS/JS/SVG, no CDN)
"""
