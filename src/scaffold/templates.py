"""Scaffold file contents for `prompt-eval init` — derived 1:1 from the
proven minimal-valid-project spec (tests/conftest.py:synthetic_project) and
the bundle envelope (src/utils/bundle.py).

Every template is valid on arrival: a fresh scaffold passes `validate`
immediately, using loudly-marked placeholders. The PLACEHOLDER token is the
teaching device — `checks` warns while any remains, `round` shows the suite
as scaffolded, and `calibrate` naturally refuses to go green on placeholder
goldens, so no paid stage can be reached on scaffold content by accident.
"""

from __future__ import annotations

PLACEHOLDER = "EVAL-INIT-PLACEHOLDER"
VIOLATION = f"{PLACEHOLDER}:VIOLATION"

# The starter contract function every scaffolded case references.
ASSERTS_FUNCTION = "starter_contract"

ASSERTS_MODULE = f'''"""Deterministic contract checks for suites scaffolded by `prompt-eval init`.

{PLACEHOLDER} — starter module. Replace {ASSERTS_FUNCTION} with real checks for
your outputs (required sections present, forbidden content absent, parseable
format, ...). Each function takes (output, context) and returns
{{"pass": bool, "score": float, "reason": str}}. Reference a function from a
dataset case as:   file://<this file>:<function>

Contracts are the free, deterministic half of grading — they run on every
tier, never drift, and catch what a judge can miss (the canary lesson).
"""


def {ASSERTS_FUNCTION}(output, context):
    """Passes on anything except the scaffold's known-bad marker or an empty
    output — just enough to prove the wiring end to end. Replace with real
    checks for your prompt's output contract."""
    text = str(output)
    if not text.strip():
        return {{"pass": False, "score": 0.0, "reason": "empty output"}}
    if "{VIOLATION}" in text:
        return {{"pass": False, "score": 0.0,
                "reason": "scaffold violation marker present (the fail golden trips this)"}}
    return {{"pass": True, "score": 1.0,
            "reason": "starter contract passed — replace it with real checks"}}
'''


def dataset_yaml(suite_id: str, prompt_file: str, asserts_ref: str,
                 fixture: str | None = None, allowed_tools: str | None = None) -> str:
    extra_vars = ""
    if fixture:
        extra_vars += f"    fixture: {fixture}\n"
    if allowed_tools:
        extra_vars += f'    allowedTools: "{allowed_tools}"\n'
    return f"""# Dataset for suite {suite_id} — one entry per test case (a realistic user
# input fed to your prompt). Scaffolded by `prompt-eval init`; every
# {PLACEHOLDER} marks a value to replace with real content.
#
# Key by key:
#   description       required — names the case in reports and validate errors
#   vars.promptFile   the prompt under test (relative to prompts/production/)
#   vars.golden       fixtures/golden/<name>/ holding pass*.txt / fail*.txt bundles
#   vars.probe        the user message — write it the way a real, busy user would
#   assert            deterministic contracts: file://<module>:<function>
#
# TRAP: never write a vars value as a YAML array — promptfoo silently explodes
# arrays into a cartesian product of test cases. Lists are comma-separated
# strings, e.g.  allowedTools: "Read,Write"
- description: "{suite_id} — {PLACEHOLDER} TODO: happy-path user request"
  vars:
    promptFile: {prompt_file}
    golden: {suite_id}
{extra_vars}    probe: "{PLACEHOLDER} — TODO: a realistic, terse user message"
  assert:
    - type: python
      value: {asserts_ref}
- description: "{suite_id} — {PLACEHOLDER} TODO: pressured or underspecified request"
  vars:
    promptFile: {prompt_file}
    golden: {suite_id}
{extra_vars}    probe: "{PLACEHOLDER} — TODO: the same intent, rushed and underspecified"
  assert:
    - type: python
      value: {asserts_ref}
"""


# Reviewed starter rubrics by task kind (`init --rubric <kind>`). Each ships
# every 5..1 anchor already written for the task shape — the user's job is to
# READ them against real outputs and edit what disagrees, not to fill blanks.
# Auto-detection is deliberately absent: a silently wrong kind would poison
# calibration semantics; the flag is an explicit human claim about the task.
RUBRIC_LIBRARY: dict[str, dict] = {
    "summarization": {
        "context": "Grade a summary of a source document. Judge only against the source "
                   "included in the bundle — outside knowledge must not rescue or punish it.",
        "dimensions": {
            "faithfulness": {
                "measures": "every claim in the summary is supported by the source",
                "anchors": {
                    5: "every statement traceable to the source; numbers, names, and causal claims exact",
                    4: "one minor imprecision (rounded number, softened claim); nothing invented",
                    3: "mostly grounded but one claim overreaches what the source says",
                    2: "several claims unsupported by the source, or one materially wrong fact",
                    1: "invents events, numbers, or conclusions — the summary misinforms",
                }},
            "coverage": {
                "measures": "the source's essential points survive into the summary",
                "anchors": {
                    5: "all load-bearing points present; omissions are genuinely peripheral",
                    4: "one secondary point missing; the reader's takeaway is unchanged",
                    3: "a main point missing or buried; takeaway partially skewed",
                    2: "multiple main points missing; the summary misrepresents emphasis",
                    1: "captures a fragment only — the reader learns the wrong story",
                }},
            "concision": {
                "measures": "information density — no padding, repetition, or throat-clearing",
                "anchors": {
                    5: "every sentence earns its place; nothing could be cut without losing content",
                    4: "a phrase or two of filler; trivially tightenable",
                    3: "noticeable repetition or preamble; ~20% could go",
                    2: "bloated — restates the source's structure instead of compressing it",
                    1: "as long as reading the source, or padded with generic commentary",
                }},
        },
    },
    "extraction": {
        "context": "Grade structured data extracted from an input document against the "
                   "requested schema. The bundle carries the input and the extraction.",
        "dimensions": {
            "field_accuracy": {
                "measures": "extracted values match the input exactly (no paraphrase, no guess)",
                "anchors": {
                    5: "every populated field verbatim-correct, normalized only as the schema asks",
                    4: "one cosmetic deviation (casing, whitespace); no wrong values",
                    3: "one wrong or paraphrased value among otherwise correct fields",
                    2: "several wrong values, or values copied from the wrong part of the input",
                    1: "values are fabricated or systematically misaligned with fields",
                }},
            "completeness": {
                "measures": "everything present in the input that the schema asks for is extracted",
                "anchors": {
                    5: "all extractable fields populated; absent data explicitly null, never guessed",
                    4: "one extractable field missed; nulls otherwise honest",
                    3: "a few extractable fields missed, or one null filled with a guess",
                    2: "many extractable fields empty while the data sits in the input",
                    1: "mostly empty or mostly guessed — extraction did not happen",
                }},
            "format_validity": {
                "measures": "the output parses and matches the requested structure",
                "anchors": {
                    5: "parses first try; keys, nesting, and types exactly as specified",
                    4: "parses; one benign deviation (extra key, harmless type coercion)",
                    3: "parses only after trivial repair (fence stripping, trailing comma)",
                    2: "structure diverges from the schema — consumers must special-case it",
                    1: "does not parse, or is prose where structure was required",
                }},
        },
    },
    "classification": {
        "context": "Grade a classification decision (label + justification) for an input. "
                   "The label set is closed; the bundle carries input and decision.",
        "dimensions": {
            "label_accuracy": {
                "measures": "the chosen label is the correct one from the allowed set",
                "anchors": {
                    5: "correct label, including on edge cases near a category boundary",
                    4: "defensible label where two categories genuinely overlap",
                    3: "plausible but second-best label; a careful rater would differ",
                    2: "wrong label a careful rater would not choose",
                    1: "wrong label, or a label outside the allowed set",
                }},
            "justification_quality": {
                "measures": "the stated reason cites the input evidence that drives the label",
                "anchors": {
                    5: "quotes or pinpoints the deciding evidence; reasoning would convince a skeptic",
                    4: "correct reasoning, evidence referenced loosely rather than pinpointed",
                    3: "generic reasoning that fits the label but ignores the specific input",
                    2: "reasoning contradicts the input or the chosen label",
                    1: "no justification, or a fabricated quote as evidence",
                }},
            "format_validity": {
                "measures": "the decision arrives in the exact requested output shape",
                "anchors": {
                    5: "exactly the requested shape; label string matches the set verbatim",
                    4: "requested shape with one benign extra (e.g. unasked confidence note)",
                    3: "recoverable deviation — label present but wrapped in prose",
                    2: "consumer must parse free text to find the label",
                    1: "no extractable label",
                }},
        },
    },
    "rag": {
        "context": "Grade an answer produced over retrieved context. Judge ONLY against "
                   "the retrieved passages in the bundle — the model's own knowledge is "
                   "not a source here.",
        "dimensions": {
            "groundedness": {
                "measures": "every claim in the answer is supported by the retrieved passages",
                "anchors": {
                    5: "all claims supported; nothing smuggled in from outside the passages",
                    4: "one connective claim beyond the passages, harmless to the conclusion",
                    3: "one substantive unsupported claim among grounded ones",
                    2: "answer leans on outside knowledge the passages do not contain",
                    1: "contradicts the passages or answers from thin air",
                }},
            "answer_completeness": {
                "measures": "the question is fully answered where the passages allow it",
                "anchors": {
                    5: "uses all relevant passages; nothing answerable left unanswered",
                    4: "one relevant detail from the passages unused",
                    3: "answers the main question but ignores a passage that qualifies it",
                    2: "partial answer while the passages support a full one",
                    1: "does not answer the question the passages can answer",
                }},
            "honest_uncertainty": {
                "measures": "gaps in the retrieved context are declared, never papered over",
                "anchors": {
                    5: "states exactly what the passages cannot answer, without hedging what they can",
                    4: "declares the gap in general terms",
                    3: "neither claims nor declares — silently answers only the covered part",
                    2: "papers over a gap with a confident-sounding generality",
                    1: "fabricates an answer for the uncovered part",
                }},
        },
    },
    "support-chat": {
        "context": "Grade one customer-support reply in context. The bundle carries the "
                   "conversation and the reply under test.",
        "dimensions": {
            "resolution": {
                "measures": "the reply moves the customer's actual problem toward resolution",
                "anchors": {
                    5: "resolves the issue or gives the exact next step with everything needed to take it",
                    4: "correct path with one missing practical detail",
                    3: "addresses the topic but the customer must ask again to proceed",
                    2: "generic advice that ignores the specifics already given",
                    1: "wrong issue, wrong product, or a brush-off",
                }},
            "accuracy": {
                "measures": "every stated fact, policy, and commitment is correct",
                "anchors": {
                    5: "all facts and policy statements correct; commitments the business can keep",
                    4: "one imprecise but harmless statement",
                    3: "one statement a customer could reasonably misread as a promise",
                    2: "a wrong policy/fact that will cause a repeat contact",
                    1: "invents policy or makes commitments that cannot be honored",
                }},
            "empathy_tone": {
                "measures": "tone matches the customer's situation — warm, never scripted",
                "anchors": {
                    5: "acknowledges the specific frustration and stays natural throughout",
                    4: "appropriate tone with one stock phrase",
                    3: "polite but template-flavored; ignores the emotional register",
                    2: "cold or mismatched (cheery reply to an angry escalation)",
                    1: "dismissive, blaming, or condescending",
                }},
            "scope_safety": {
                "measures": "stays inside support scope — no legal/medical advice, no data leaks",
                "anchors": {
                    5: "cleanly in scope; redirects out-of-scope asks appropriately",
                    4: "in scope; one borderline aside handled adequately",
                    3: "drifts out of scope harmlessly",
                    2: "gives advice or discloses details the role must not",
                    1: "leaks other-customer data or makes prohibited claims",
                }},
        },
    },
    "codegen": {
        "context": "Grade generated code against the stated task. The bundle carries the "
                   "task and the code (plus any files created).",
        "dimensions": {
            "correctness": {
                "measures": "the code does what the task asks, including edge cases",
                "anchors": {
                    5: "correct on the main path and stated edge cases; no latent bug on plain reading",
                    4: "correct main path; one unhandled edge case outside the task's core",
                    3: "works for the happy path only; a stated requirement missed",
                    2: "runs but produces wrong results for common inputs",
                    1: "does not run, or solves a different problem",
                }},
            "idiomatic_style": {
                "measures": "reads like the target language/codebase — naming, structure, idiom",
                "anchors": {
                    5: "a maintainer would merge it as-is; idiomatic throughout",
                    4: "minor style nits only",
                    3: "works but fights the language (reinvented stdlib, odd structure)",
                    2: "hard to follow; misleading names or tangled control flow",
                    1: "unreadable or transliterated from another language",
                }},
            "scope_discipline": {
                "measures": "changes only what the task requires — no drive-by rewrites",
                "anchors": {
                    5: "touches exactly the needed surface; no unrelated churn",
                    4: "one small defensible extra (e.g. fixing a typo it touched)",
                    3: "noticeable unrequested refactoring alongside the task",
                    2: "rewrites working code unrelated to the ask",
                    1: "large unrelated changes dominate the diff",
                }},
            "safety": {
                "measures": "no injection, secret-handling, or destructive-operation hazards",
                "anchors": {
                    5: "inputs validated where they cross trust boundaries; no dangerous defaults",
                    4: "safe in context; one hardening opportunity missed",
                    3: "safe only under assumptions the code never states",
                    2: "a real hazard on plausible input (injection, path traversal, eval)",
                    1: "hardcodes secrets or performs destructive operations unguarded",
                }},
        },
    },
    "rewrite": {
        "context": "Grade a rewrite of a source text toward a stated goal (tone, length, "
                   "audience). The bundle carries the source, the goal, and the rewrite.",
        "dimensions": {
            "meaning_preservation": {
                "measures": "the rewrite says what the source says — nothing added, lost, or bent",
                "anchors": {
                    5: "all facts, commitments, and nuances intact",
                    4: "one nuance flattened; no factual drift",
                    3: "one factual detail dropped or subtly changed",
                    2: "meaningful claims added or removed",
                    1: "says something different from the source",
                }},
            "goal_adherence": {
                "measures": "the rewrite actually achieves the requested transformation",
                "anchors": {
                    5: "unmistakably in the requested register/length/audience throughout",
                    4: "achieves the goal with one lapse into the source's register",
                    3: "halfway — direction right, transformation incomplete",
                    2: "token gestures at the goal; substantially the source restyled",
                    1: "ignores the requested transformation",
                }},
            "fluency": {
                "measures": "the result reads as if written fresh, not edited",
                "anchors": {
                    5: "natural throughout; no seams from the source's structure",
                    4: "one awkward transition betrays the edit",
                    3: "readable but visibly patched together",
                    2: "grammatical errors or broken flow introduced by the rewrite",
                    1: "garbled or self-contradicting text",
                }},
        },
    },
}


def rubric_md(suite_id: str, kind: str | None = None) -> str:
    if kind is not None:
        lib = RUBRIC_LIBRARY[kind]
        out = [f"# Rubric: {suite_id}",
               "",
               f"{PLACEHOLDER} — starter rubric from the `{kind}` template. These anchors",
               "are a reviewed starting point, not your quality bar: read each one against",
               "real outputs of YOUR prompt, edit what disagrees, then delete this line.",
               "",
               lib["context"],
               ""]
        for name, dim in lib["dimensions"].items():
            out += [f"## dimension: {name}",
                    f"What it measures: {dim['measures']}"]
            out += [f"- {level}: {dim['anchors'][level]}" for level in (5, 4, 3, 2, 1)]
            out += [""]
        return "\n".join(out)
    dims = {
        "correctness": "whether the output does what the prompt promises, factually and precisely",
        "completeness": "whether every part of the user's request is addressed",
        "clarity": "whether the output is well-organized and immediately usable",
    }
    out = [f"# Rubric: {suite_id}",
           "",
           f"{PLACEHOLDER} — starter rubric scaffolded by `prompt-eval init`. Rewrite the",
           "dimensions and anchors for YOUR quality bar: dimension names must be",
           "snake_case, 3-5 dimensions are recommended, and every dimension needs all",
           "five anchors (5..1) — the parser hard-requires them. Anchors are the",
           "calibration contract: a judge is only trusted after `calibrate` proves it",
           "scores your golden fixtures inside these bands.",
           ""]
    for name, measures in dims.items():
        out += [f"## dimension: {name}",
                f"What it measures: {measures}",
                f"- 5: exemplary — {PLACEHOLDER} TODO: describe what a 5 looks like for {name}",
                f"- 4: good with minor gaps — TODO: the 4 bar for {name}",
                f"- 3: acceptable but flawed — TODO: the 3 bar for {name}",
                f"- 2: substantially deficient — TODO: the 2 bar for {name}",
                f"- 1: unusable — TODO: the 1 bar for {name}",
                ""]
    return "\n".join(out)


def golden_pass(suite_id: str, with_file_section: bool = False) -> str:
    out = f"""===== STDOUT =====
{PLACEHOLDER} — replace this with a REAL known-good output of your prompt
(copy one from an actual run). Keep the "===== STDOUT =====" header: asserts
and judges read this envelope. This placeholder deliberately satisfies the
starter contract so `validate` is green from minute one; `calibrate` will
refuse to trust a judge on placeholder goldens, so nothing paid can build on
this file by accident.
"""
    if with_file_section:
        out += f"""===== FILE: example-artifact.md =====
{PLACEHOLDER} — agent suites also judge files the agent created or modified;
a bundle carries each one in a "===== FILE: <path> =====" section like this.
Replace with the real artifact a good run produced.
"""
    return out


def golden_fail(suite_id: str) -> str:
    return f"""===== STDOUT =====
{VIOLATION} — replace this with a REALISTIC known-bad output (one a broken
prompt might actually produce: missing sections, wrong tone, leaked
instructions...). Every fail golden must trip at least one contract —
`validate` proves your checks can actually catch bad output. This marker
trips the scaffold's starter contract until you write real checks.
"""


def graded_spec_yaml(suite_id: str, prompt_file: str, asserts_ref: str,
                     fixture: str | None = None, allowed_tools: str | None = None) -> str:
    base_extra = ""
    if fixture:
        base_extra += f"  fixture: {fixture}\n"
    if allowed_tools:
        base_extra += f'  allowedTools: "{allowed_tools}"\n'
    return f"""# Graded-tier spec for {suite_id} — `prompt-eval matrix` composes the axes
# below into matrix cells (full product up to graded.max_cells_per_suite,
# pairwise reduction beyond) and writes datasets/graded/{suite_id}.yaml.
# A fraction of cells (graded.holdout) is reserved for post-promotion
# confirmation only. Scaffolded by `prompt-eval init`: replace every
# {PLACEHOLDER} probe with real user phrasing along axes that matter to YOU
# (tone, specificity, length, adversarial pressure, ...).
suite: {suite_id}
base:
  promptFile: {prompt_file}
  golden: {suite_id}
{base_extra}contracts:
  - type: python
    value: {asserts_ref}
axes:
  tone:
    - id: polite
      probe: "{PLACEHOLDER} — TODO: a polite phrasing of a real request"
    - id: rushed
      probe: "{PLACEHOLDER} — TODO: the same request, terse and hurried"
  detail:
    - id: vague
      probe: "{PLACEHOLDER} — TODO: the request with details missing"
    - id: specific
      probe: "{PLACEHOLDER} — TODO: the request fully specified"
"""


def workspace_readme(suite_id: str) -> str:
    return f"""{PLACEHOLDER} — fixture workspace for suite {suite_id}.

Every live trial gets a FRESH copy of this directory as its working dir, so
trials never see each other's files. Put here whatever your agent prompt
expects to find on disk (input documents, configs, starter code). Files the
agent CREATES or MODIFIES relative to this baseline are packaged into the
judged bundle as FILE sections.
"""


def wrapper_config_block(prompt_file: str, wrapper_file: str,
                         wrappers_dir: str = "prompts/wrappers") -> str:
    """The prompts.wrappers block inserted when a wrapped prompt arrives and
    the config has none yet (template recovered from the pre-bare config)."""
    return f"""  wrappers:
    # Production fidelity: test the prompt the way your product actually
    # invokes it — the wrapper's boundary text is appended at eval time.
    dir: {wrappers_dir}
    strip_frontmatter: true
    strip_lines_matching: []
    heading: "Boundary reminders (from the production wrapper)"
    map:
      {prompt_file}: {wrapper_file}"""


INPUT_README = """# input/ — the drop box

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
"""
