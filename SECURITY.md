# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately via
[GitHub Security Advisories](https://github.com/AlqarniMohammed/prompt-eval-engine/security/advisories/new)
— do not open a public issue for anything exploitable. You should get a
response within a week.

## Scope and threat model

- **API keys.** The engine reads `ANTHROPIC_API_KEY` (and optionally
  `OPENAI_API_KEY`) from `.env`/environment only. Keys are never written to
  run artifacts: durable records pass through the secret scrubber
  (`src/utils/redact.py`), and HTTP-provider header values are masked in
  manifests. Anything that gets a key into a tracked or durable file is a
  vulnerability — report it.
- **Bundles execute code by design.** Adopting a third-party bundle via
  `init` means trusting its Python: its contracts module and any
  `file://module.py:...` dataset references run as ordinary code, starting
  with the free `validate` (see `input/README.md`). This is documented,
  intended behavior — equivalent to installing a package — not a
  vulnerability. Sandbox escapes from anything that promises isolation would
  be; nothing here promises isolation.
- **The dashboard** is read-only, GET-only, and binds `127.0.0.1` only. A
  write path, a bind beyond localhost, or an XSS from artifact content would
  each be a vulnerability.

## Supported versions

v1.x only (the project is bug-fixes-only; fixes land on `main`).
