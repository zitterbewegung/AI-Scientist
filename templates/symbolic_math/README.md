# Symbolic Math Template

A symbolic law discovery harness for The AI Scientist: the experiment searches
over symbolic expression trees to recover hidden closed-form laws (Kepler's
third law, pendulum period, ideal gas, radioactive decay, Snell refraction, and
a deliberately hard large-angle pendulum correction) from sampled data.

Two things make this template math-specific:

1. **The object of study is the search technique itself.** The baseline
   `propose_candidate` is uniform random search; ideas generated against this
   template propose new mathematical search techniques (annealing, evolutionary
   edits, canonicalization, dimensional pruning, Pareto complexity selection,
   ...) and are judged on solve rate, verified rate, and evaluations-to-solve.
2. **Results are symbolically verified, not just fit.** A candidate that
   reaches NRMSE < 1e-6 is checked with sympy to be *provably equal* to the
   ground truth up to a constant factor (`verified`). The baseline's
   `ideal_gas_pressure` near-miss (`x1*(-3*x2 + x3 - 1)/x3` at NRMSE 0.0028)
   shows why the gate exists: a looser numeric threshold would admit
   structurally wrong laws that only the symbolic check can reject. This is
   the template-level analogue of the formal verification gate described in
   `docs/lean_verifier_plan.md`.

## Usage

Set up and generate the baseline (CPU-only, a few seconds). The install must
go into the **same environment that runs `launch_scientist.py`** — the
pipeline runs `python experiment.py` as a bare subprocess, and the repo-level
environment does not otherwise pull in sympy:

```bash
cd templates/symbolic_math
pip install -r requirements.txt   # numpy, sympy, matplotlib
python experiment.py --out_dir=run_0
python plot.py
```

Note: `run_0/` is gitignored by the repo (`templates/*/run_0/`), so the
baseline must be regenerated per machine (it takes seconds and is
deterministic), or force-added with `git add -f` if you want it to ship.

Then launch The AI Scientist as usual:

```bash
python launch_scientist.py --model "claude-3-5-sonnet-20240620" --experiment symbolic_math --num-ideas 2
```

## Metrics (per benchmark, mean/stderr over 3 seeds)

- `solved`: best candidate reached NRMSE < 1e-6 after scale fitting
- `verified`: best candidate proven symbolically equal to the truth up to a
  constant factor
- `best_nrmse`: final best normalized RMSE (clipped at 10)
- `evals_to_solve`: candidate evaluations until solved (budget = 3000 if never)
- `best_complexity`: node count of the best expression tree

An `aggregate` entry reports solve/verify rates and mean NRMSE across all
benchmarks. The baseline solves 4/6 benchmarks on at least one seed
(`ideal_gas_pressure` on only one of three), aggregate solve rate ~0.56;
`snell_refraction` and `large_angle_pendulum` are unsolved headroom.
