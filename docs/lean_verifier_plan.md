# Lean Verifier Integration Plan

Goal: extend The AI Scientist so that mathematical claims it discovers are
**machine-verified before they are written up** — culminating in Lean 4 formal
proofs — and so that inventing new proof/search techniques becomes something the
system can experiment on, not just talk about.

Design principle: *nothing unverified reaches the writeup*. The pipeline's
existing reviewer (`ai_scientist/perform_review.py`) is an LLM paper reviewer —
it judges plausibility, not validity. For mathematics the verifier is the whole
ballgame: a plausible-but-wrong derivation must be caught mechanically, not
editorially.

Status of the tiers:

| Tier | Verifier | Status |
|------|----------|--------|
| 1. Numeric | NRMSE fit on sampled data | done (`templates/symbolic_math`) |
| 2. Symbolic | sympy proof of equality up to constant | done (`templates/symbolic_math`) |
| 3. Formal | Lean 4 + mathlib proof | this plan |

The baseline run already illustrates why the higher tiers exist: it produced
`x1*(-3*x2 + x3 - 1)/x3` for the ideal-gas law at NRMSE 0.0028 — numerically
plausible, structurally wrong. Tier 1's tight threshold rejects it today, but
any looser threshold (noisy data, harder laws) would admit it, and only the
symbolic tier can tell fit from truth.
Tier 3 is the same idea with a sound checker and a vastly larger scope
(inequalities, asymptotics, number theory — anything statable in mathlib).

## 1. Where this hooks into the v1 pipeline

The pipeline is template-driven and the touch points are small:

- `launch_scientist.py:197` — `fnames = [exp_file, vis_file, notes]` is the
  aider edit surface. **Patch**: if the template contains `editable_files.json`
  (a list of extra relative paths, e.g. `["Conjectures/Discovered.lean"]`),
  append those to `fnames`. Backwards-compatible: absent file → current
  behavior. ~10 lines.
- `ai_scientist/perform_experiments.py:30` — `run_experiment` plus the retry
  loop in `perform_experiments()` (`perform_experiments.py:126-138`) already
  have exactly the right shape for a proof loop: run a command, feed the stderr
  tail back to the coder on failure, retry up to `MAX_ITERS`. A Lean template reuses it
  unchanged; only the template's `experiment.py` differs. The `timeout=7200`
  default is fine for REPL-checked runs; make it a template-configurable
  constant only if Aristotle-tier runs need more (see §4).
- `launch_scientist.py` `do_idea`, between experiments and writeup — **the
  verification gate**. Semantics matter here, so define them precisely. A
  *claim* is a result the writeup would assert: a theorem presented as proved,
  or a refutation presented with its counterexample. Conjectures that remain
  `open` are the normal outcome of most runs — they are reportable *as open*
  and never trip the gate. The gate's behavior is **prune-to-verified**, not
  hard-fail: inject `verification_report.json` (written by the template's
  `experiment.py`) into `notes.txt`, instructing the writeup to present
  verified results as results and everything else explicitly as open/failed.
  Hard-fail (skip writeup, as when experiments fail) is reserved for a
  missing report — hard-failing on any unverified item would discard
  hours of verified Aristotle results because `do_idea` returning False
  abandons the idea entirely (`launch_scientist.py:227-229`). ~15 lines.

Everything else lives inside a new template, which the pipeline treats like any
other.

## 2. New template: `templates/lean_conjectures/`

```
templates/lean_conjectures/
├── experiment.py          # orchestrates: generate → attempt → check → score
├── plot.py                # proved/refuted/open counts, time-to-proof curves
├── prompt.json            # task: invent better conjecture→proof strategies
├── seed_ideas.json
├── editable_files.json    # ["Conjectures/Discovered.lean", "prover_config.json"]
├── requirements.txt       # lean-interact, aristotlelib (optional), sympy, numpy
├── latex/
└── LeanProject/
    ├── lakefile.toml      # pinned mathlib
    ├── lean-toolchain     # pinned toolchain (see §6)
    └── Conjectures/
        ├── Statements.lean   # conjecture pool: theorems ending in `sorry`
        └── Discovered.lean   # where proved results accumulate
```

`experiment.py` loop, per run:

1. **Load conjectures** — Lean theorem statements with `sorry` bodies. Initial
   pool: exports from the `symbolic_math` template (§5) plus a curated set of
   graded mathlib-style lemmas (so metrics are populated from day one).
2. **Refutation pass** — before spending prover budget, run mathlib's
   `plausible` (counterexample search) against each statement. Refuted
   statements are *results too* (score them separately) and this is the guard
   against vacuous or mis-formalized conjectures. Capture the counterexample
   text itself in the per-conjecture output record, and write a
   `refuted_statements.json` sidecar that subsequent runs load so refuted
   conjectures are never re-attempted (thesisus takes over this memory role
   at M7).
3. **Prover ladder** (§3) — cheap tactics first, escalate on failure.
4. **Check** — every returned proof is re-elaborated through the Lean REPL.
   Trust the checker, never the prover. Scan for residual `sorry`/`admit`
   before counting a theorem as proved (Aristotle's documented failure mode is
   proving helper lemmas while the main goal stays `sorry`).
5. **Score** — `final_info.json` in the standard format, per conjecture family:
   `proved`, `refuted`, `open`, `proof_wall_seconds`, `proof_length_lines`,
   `ladder_level_used` (means/stderrs across seeds where stochastic), plus a
   per-conjecture record (statement, status, prover rung, proof or
   counterexample text) in the run folder for the gate and for thesisus.

The critical performance decision: **check proofs via the Lean REPL, not
`lake build`**. `leanprover-community/repl` speaks JSON over stdin/stdout; you
pay `import Mathlib` once (tens of seconds, ~5 GB RAM), then each candidate
proof elaborates in seconds against a resident environment, with structured
error messages, `sorries`, and goal states back. `pip install lean-interact`
wraps exactly this for Python (supports Lean v4.8–v4.32):

```python
from lean_interact import LeanREPLConfig, LeanServer, Command, LocalProject

server = LeanServer(LeanREPLConfig(project=LocalProject(directory="LeanProject")))
resp = server.run(Command(cmd=theorem_with_candidate_proof, env=mathlib_env))
proved = not resp.has_errors() and not resp.sorries
```

(API names verified against lean-interact 0.11.5.)

A naive `lake build` per candidate (minutes each) makes agent loops infeasible;
the REPL makes them cheap. (At larger scale, Kimina Lean Server is the same
idea as a parallel FastAPI service — a later optimization, not needed at first.)

What the AI Scientist experiments on here: the *strategy* — conjecture
selection, statement decomposition into lemmas, tactic-sketch generation,
premise selection, repair loops (the APOLLO pattern: localize the failing step,
isolate it as a lemma, retry, recombine). That is "inventing proof techniques"
made operational, with proved-theorem counts as the metric.

## 3. Prover backends: Aristotle vs LeanCopilot (answer: both, different rungs)

Research findings (August 2026), then the recommendation.

**Harmonic Aristotle** — cloud service, frontier strength (IMO 2025
gold-equivalent, 5/6 problems formally verified; accepted mathlib
contributions). Access is now self-serve: sign up at aristotle.harmonic.fun,
API key from the dashboard, `pip install aristotlelib` (v2.1.0, Python ≥3.10),
and the terms state Harmonic *does not presently charge fees*. You submit Lean
4 code containing `sorry`s (imports auto-added, project context supported) or
even informal LaTeX for autoformalization; results return only after the proof
verifies. It can also return explicit counterexamples for false statements.
Costs and caveats: latency is minutes-to-hours (a published case study logged
~8 h on one hard problem) so submission must be async — in the v2.x SDK that
means `Project.create` / `Project.create_from_directory`, then `project.ask(...)`
and `task.wait_for_completion()` (or polling `ProjectStatus`), or simply the
CLI `aristotle submit "Fill in all sorries" --project-dir LeanProject --wait`.
Beware stale examples: `Project.prove_from_file`, still shown in third-party
wrappers, was removed after v0.1.0. The SDK had a breaking 2.0.0 bump in May
2026 and has no public source repository, so pin the version and verify
signatures against the installed wheel before wiring anything; no published
rate limits or toolchain pins — probe empirically; ToS prohibit using outputs
to train models and submitting personal information; and your conjectures
leave the machine — fine for this project, but it is an externalization
decision. **Secrets**: the key lives only in the `ARISTOTLE_API_KEY`
environment variable (the repo's existing convention for `OPENAI_API_KEY` and
`S2_API_KEY`), never in any template file — `prover_config.json` is on the
aider edit surface and `do_idea` copytrees the template into every idea
folder, so a key written there would be LLM-editable and copied everywhere.

**LeanCopilot** (lean-dojo) — the opposite profile: local, free, open source,
CPU-capable, with a 32 MB arm64-macOS prebuilt. It lives *inside* Lean as
tactics (`suggest_tactics`, `search_proof`, `select_premises`), so the batch
pattern is: write `theorem c_17 : … := by search_proof`, elaborate via the
REPL, harvest the "Try this" suggestion on success. Its bundled models are
ReProver-class ByT5 (~300 M params): ~26.5% miniF2F whole-proof — genuinely
useful for routine/library-style goals, nowhere near frontier (the headline
"74.2%" is proof-*step* automation, a different metric). Each release pins one
Lean toolchain (v4.14.0–v4.32.0 available; actively maintained). LeanDojo v1
(the Python gym) is deprecated and needs statements committed to a traced repo
— wrong fit for generated conjectures; LeanDojo-v2 is CUDA/Linux-oriented.
For this pipeline, LeanCopilot-as-tactic + REPL is the right consumption mode.

**The ladder** (each rung a `prover_config.json` entry, escalate on failure):

1. `aesop` / `exact?` / `simp` / `omega` / `norm_num` / `nlinarith` — free,
   ~1 s per goal, closes trivia. (LeanHammer is a stronger ~33–40% option on
   mathlib-shaped goals if we want a rung 1.5.)
2. LeanCopilot `search_proof` — local neural search, seconds-to-minutes,
   closes routine lemmas.
3. The pipeline's own LLM writing proofs through the aider loop, with REPL
   errors as feedback — this is the rung the AI Scientist itself can innovate
   on (repair strategies, lemma decomposition), and Claude-class models are
   respectable Lean users in 2026.
4. Aristotle — async, hours, frontier. Reserve for statements that survive
   rungs 1–3 and matter (final results headed for the writeup).
   Self-hosted alternative if external submission is unacceptable or the
   service changes: Goedel-Prover-V2-8B (Apache-2.0, 83–84.6% miniF2F pass@32
   — the HF model card and GitHub README disagree; single 24–80 GB GPU)
   served via vLLM — but that's a hardware commitment; start with Aristotle.

This ordering keeps the fast loop fast (rungs 1–2 are local and cheap enough
to run inside `experiment.py`'s 5-run budget) while making frontier proving a
deliberate, budgeted escalation rather than a blocking dependency.

## 4. Aristotle latency vs the pipeline's timeouts

`run_experiment`'s 7200 s timeout is incompatible with 8-hour Aristotle jobs
if handled synchronously. Handle it with a **submit-and-collect** pattern
inside the template: `experiment.py` submits rung-4 jobs, records
`aristotle_pending` in `final_info.json`, and each subsequent run (or a final
`collect` pass) polls and folds completed proofs in. For a v1, simpler is
fine: cap Aristotle wait time at 60–90 min per statement, accept that some
jobs report as `open`, and let a human (or a later run) harvest stragglers
with `aristotle download`.

## 5. The bridge: symbolic_math → Lean

Discovered laws from `templates/symbolic_math` are equalities over a tiny op
grammar (`+ − × ÷ sqrt sin cos exp log neg`, positive-range hypotheses), so a
sympy→Lean printer is mechanical — this is *autoformalization by construction*,
sidestepping the landscape's weakest link (LLM autoformalization can
mis-formalize, and a mis-formalized statement can be vacuously "proved";
Aleph's PutnamBench run found ~2% of formal statements were wrong). Example
export of the verified pendulum discovery:

```lean
theorem pendulum_form (x1 x2 : ℝ) (h1 : 0 < x1) (h2 : 0 < x2) :
    2 * Real.pi * Real.sqrt (x1 / x2)
      = 2 * Real.pi * (Real.sqrt x1 / Real.sqrt x2) := by
  rw [Real.sqrt_div' x1 (le_of_lt h2)]  -- or: by field_simp [Real.sqrt_div]
```

Sympy's "verified up to constant" becomes a clean equational lemma with
explicit positivity hypotheses; rung-1 tactics or LeanCopilot should close
most of these, making the bridge an ideal first corpus: real discovered
content, mostly provable, with a difficulty tail.

## 6. Toolchain bootstrap (this machine has no Lean yet)

`elan`/`lake` are not installed. One-time setup (~1 h, mostly mathlib cache):

```bash
brew install elan-init git-lfs && elan default stable
# pin: pick the newest toolchain both tools support. As of Aug 2026:
#   - lean-interact documents support through v4.32.0-rc1 (stable v4.32.0 is
#     likely fine but verify before committing to it)
#   - LeanCopilot releases exist for v4.14.0–v4.32.0, BUT v4.28.0–v4.31.x are
#     a documented dead zone (downstream bug, fixed in v4.32.0-rc1) — pin
#     v4.32.0/v4.32.0-rc1 or drop to ≤ v4.27
# → lean-toolchain + mathlib pinned to the chosen version, together
cd templates/lean_conjectures/LeanProject
lake update && lake exe cache get          # mathlib binary cache, several GB
lake exe LeanCopilot/download              # ~/.cache/lean_copilot models
pip install lean-interact aristotlelib
```

Version lockstep (project ↔ mathlib ↔ LeanCopilot ↔ lean-interact) is the
classic Lean-ecosystem hazard: pin all four in the template and treat upgrades
as a deliberate maintenance task.

## 7. Thesisus integration (yes — as the memory layer, at the boundary)

[thesisus](https://github.com/aconsapart/thesisus) (local-first theorem
workbench: SQLite "theorem codex" + CLI/TUI/Datasette/Streamlit + LangGraph
agents) solves exactly the thing this v1 pipeline lacks: **persistence across
runs**. Each `do_idea` folder is an island; conjectures proved, refuted, or
left open in one run are invisible to the next.

Integrate at the boundary, not inside templates (templates must stay
self-contained — the pipeline copytrees them and runs them headless, and heavy
imports inside `experiment.py` would burden every idea run):

- **Outbound**: templates already emit everything needed —
  `final_info.json`, `verification_report.json`, discovered expressions,
  Lean sources of proved theorems. Add a thesisus ingest command
  (`thesius ingest-ai-scientist results/<template>/<idea>/`) that loads
  statements, proof status, provenance (template, idea name, run, prover rung),
  and the proof text into the codex.
- **Inbound**: a `thesius export-conjectures` command emits open conjectures
  as `Statements.lean` + a JSON manifest that `templates/lean_conjectures`
  consumes as its conjecture pool, and its statistics (what got proved by which
  rung) can seed `seed_ideas.json` for strategy ideas.
- This makes the combined system a loop: discover (symbolic_math) → formalize
  (bridge) → prove/refute (lean_conjectures) → remember (thesisus) → re-seed.

Keep the coupling to those two commands; don't make the pipeline import
thesisus or vice versa.

## 8. Milestones

| # | Deliverable | Effort |
|---|-------------|--------|
| M1 | `templates/symbolic_math` (tiers 1–2) | **done** |
| M2 | LeanProject skeleton + REPL check harness (`lean-interact`), rung-1 tactics, graded statement pool | 2–3 days |
| M3 | `editable_files.json` patch + verification gate in `do_idea` | ½ day |
| M4 | sympy→Lean exporter + bridge corpus from symbolic_math results | 1 day |
| M5 | LeanCopilot rung (lakefile dep, model download, "Try this" harvesting) | 1 day |
| M6 | Aristotle rung (async submit/poll, residual-sorry check, version-pinned SDK) | 1–2 days |
| M7 | thesisus ingest/export commands | 1–2 days |
| M8 | `plausible` refutation pass + refuted-conjecture metrics | ½ day |

M2–M4 alone yield a working formally-verified template; M5–M8 are independent
increments in any order.

## 9. Risks

- **Version lockstep** (Lean/mathlib/LeanCopilot/lean-interact): pin
  everything; upgrade deliberately.
- **Aristotle is a startup service**: currently free — that will change;
  SDK broke compatibility once already; keep rung 3 (own-LLM proving) healthy
  as the fallback and treat rung 4 as optional acceleration.
- **REPL memory**: ~5 GB per mathlib environment; run one server, reuse env
  ids, don't parallelize checks naively.
- **Domain gap**: all provers are trained on competition/mathlib-style
  statements; expect the bridge corpus (real analysis over `sqrt`/`sin`) to be
  *easier* than competition math but occasionally weird — the ladder plus
  `plausible` covers both ends.
- **Metric gaming**: "proved count" can be inflated by trivial conjectures;
  score conjecture pools by a fixed rubric (statement depth, refuted-vs-proved
  balance) and keep the pool fixed within a comparison, exactly as
  `symbolic_math` fixes its benchmark suite.
