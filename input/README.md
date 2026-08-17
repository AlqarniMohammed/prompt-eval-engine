# input/ — the drop box

Drop a prompt here and run `uv run prompt-eval init` — the engine detects
what you dropped, moves it into place, scaffolds everything the pipeline
needs (dataset, rubric, golden fixtures, graded spec, contracts), registers
the suite in `config/eval_config.yaml`, and prints what each file proves.
`validate` is green immediately; every placeholder you then replace upgrades
one link of the proof. Nothing here costs money until you run `calibrate`.

What you can drop:

| You drop | Detected as | What init does |
|---|---|---|
| `my-prompt.md` | bare prompt | scaffolds a full suite around it |
| a folder with prompt + wrapper `.md` | wrapped prompt | + registers the wrapper map |
| a `.md` using tools / a folder with prompt + support files | agent prompt | + fixture workspace, `allowedTools`, file-section golden |
| a folder with dataset/rubric/goldens | bundle | validates everything, then registers it as-is |

Ambiguity (bare vs agent prompt) is resolved with one interactive question —
or non-interactively with `--as prompt|wrapped|agent|bundle` / `--yes`.
Optionally add an `input.yaml` next to a folder's content to pin the answers:

```yaml
type: agent            # prompt | wrapped | agent | bundle
suite: my-suite-id     # default: derived from the file/folder name
allowedTools: "Read,Write"
fixture: my-workspace  # name for the fixture workspace directory
wrapper: wrapper.md    # which .md is the wrapper (type: wrapped)
```

`init` never overwrites: existing files are kept, a suite id that already
exists with different content is a hard error, and `--dry-run` shows the
plan without touching anything.

**Adopting a bundle means trusting its code.** A bundle ships a `contracts.py`
whose functions run as ordinary Python — starting with the very first
`validate`, which imports the module even though it makes no paid calls. The
same applies to any `file://module.py:...` reference in a bundle's dataset.
Read a third-party bundle's Python before running `init` on it, exactly as you
would read a script before executing it.
