"""The input/ drop-box adopter (`prompt-eval init`).

A developer drops a prompt (or a folder) into input/; `init` detects what it
is, scaffolds every file the pipeline needs (valid on arrival — `validate` is
green immediately, on loudly-marked placeholders), registers the suite in
eval_config.yaml, and teaches what each emitted file proves.

Modules:
    detect       structural detection: prompt | wrapped | agent | bundle
    templates    scaffold file contents (derived from the proven minimal
                 valid project spec in tests/conftest.py:synthetic_project)
    emit         create-if-absent file emission + the move ledger
    config_edit  anchored textual edits to eval_config.yaml with semantic
                 verification, .bak backup, and rollback
    register     per-item orchestration: emit + config + matrix + teaching
"""
