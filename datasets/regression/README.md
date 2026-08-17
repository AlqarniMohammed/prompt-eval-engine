# Permanent regression sets

One `<suite>.yaml` per suite, written by `prompt-eval pin --run <dir> --cell <id>`
— each case is a verbatim copy of a graded-matrix cell that once failed, stamped
`regression: "true"`.

Rules the engine enforces:

- Regression cases run in **every** graded-tier pass (`graded`, `compare`,
  `confirm` — including confirm's holdout-only pass).
- Any regression-case failure fails the run loudly (exit 1) regardless of the
  aggregate score, blocks stage recording, and forces a compare REJECT when the
  candidate side re-breaks one.
- The file is part of the suite's `validated` fingerprint: after pinning, re-run
  the free `validate`, then `graft`.
