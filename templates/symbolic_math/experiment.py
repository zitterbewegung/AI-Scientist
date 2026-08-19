import argparse
import json
import os

import numpy as np
import sympy as sp

# Symbolic law discovery: recover closed-form mathematical laws from sampled data
# by searching over expression trees. The search strategy in `propose_candidate`
# is intentionally simple (uniform random search) and is the primary target for
# improvement: better proposal distributions, mutation/crossover, annealing,
# complexity control, dimensional analysis, etc.
#
# A discovered law is scored numerically (normalized RMSE after fitting a single
# multiplicative constant) and, if it fits to high precision, checked *symbolically*
# against the hidden ground truth with sympy. "verified" therefore means the
# discovered expression is provably equal to the true law up to a constant factor,
# not merely a good numerical fit.

EVALS_PER_SEED = 3000
NUM_SEEDS = 3
NUM_SAMPLES = 256
MAX_DEPTH = 5
SOLVE_TOL = 1e-6
RECORD_EVERY = 50
NRMSE_CLIP = 10.0

BINARY_OPS = {
    "add": np.add,
    "sub": np.subtract,
    "mul": np.multiply,
    "div": np.divide,
}
UNARY_OPS = {
    "neg": np.negative,
    "sqrt": np.sqrt,
    "sin": np.sin,
    "cos": np.cos,
    "exp": np.exp,
    "log": np.log,
}
SYMPY_BINARY = {
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "mul": lambda a, b: a * b,
    "div": lambda a, b: a / b,
}
SYMPY_UNARY = {
    "neg": lambda a: -a,
    "sqrt": sp.sqrt,
    "sin": sp.sin,
    "cos": sp.cos,
    "exp": sp.exp,
    "log": sp.log,
}
CONSTS = [1, 2, 3]

X1, X2, X3 = sp.symbols("x1 x2 x3", positive=True)
ALL_VARS = [X1, X2, X3]

# Each benchmark hides a ground-truth law; the search only sees sampled (X, y) data.
# Ranges are chosen so every law is well-defined and smooth on the sampled domain.
BENCHMARKS = {
    "kepler_third_law": {
        "expr": X1 ** sp.Rational(3, 2),
        "ranges": [(0.5, 4.0)],
    },
    "pendulum_period": {
        "expr": 2 * sp.pi * sp.sqrt(X1 / X2),
        "ranges": [(0.1, 2.0), (5.0, 15.0)],
    },
    "ideal_gas_pressure": {
        "expr": X1 * X2 / X3,
        "ranges": [(1.0, 10.0), (200.0, 400.0), (1.0, 10.0)],
    },
    "radioactive_decay": {
        "expr": sp.exp(-X1),
        "ranges": [(0.0, 3.0)],
    },
    "snell_refraction": {
        "expr": sp.sin(X1) / sp.sin(X2),
        "ranges": [(0.2, 1.3), (0.2, 1.3)],
    },
    "large_angle_pendulum": {
        "expr": 2 * sp.pi * sp.sqrt(X1 / X2) * (1 + X3 ** 2 / 16),
        "ranges": [(0.1, 2.0), (5.0, 15.0), (0.1, 1.5)],
    },
}


def random_tree(rng, n_vars, max_depth):
    if max_depth <= 1 or rng.random() < 0.3:
        if rng.random() < 0.75:
            return ("var", int(rng.integers(n_vars)))
        return ("const", int(rng.choice(CONSTS)))
    if rng.random() < 0.55:
        op = list(BINARY_OPS)[rng.integers(len(BINARY_OPS))]
        return (op, random_tree(rng, n_vars, max_depth - 1), random_tree(rng, n_vars, max_depth - 1))
    op = list(UNARY_OPS)[rng.integers(len(UNARY_OPS))]
    return (op, random_tree(rng, n_vars, max_depth - 1))


def propose_candidate(rng, n_vars, history):
    """Propose the next candidate expression tree.

    Baseline strategy: uniform random search, ignoring `history`.

    This is the main plug point for new search techniques. `history` is a list of
    (tree, nrmse) tuples for all candidates evaluated so far this run (nrmse is
    np.inf for invalid candidates), so evolutionary, local-search, or
    learning-based strategies can be implemented here without touching the loop.
    """
    return random_tree(rng, n_vars, MAX_DEPTH)


def eval_tree(tree, X):
    op = tree[0]
    if op == "var":
        return X[:, tree[1]]
    if op == "const":
        return np.full(X.shape[0], float(tree[1]))
    if op in UNARY_OPS:
        return UNARY_OPS[op](eval_tree(tree[1], X))
    return BINARY_OPS[op](eval_tree(tree[1], X), eval_tree(tree[2], X))


def tree_complexity(tree):
    if tree[0] in ("var", "const"):
        return 1
    return 1 + sum(tree_complexity(c) for c in tree[1:])


def tree_to_sympy(tree):
    op = tree[0]
    if op == "var":
        return ALL_VARS[tree[1]]
    if op == "const":
        return sp.Integer(tree[1])
    if op in SYMPY_UNARY:
        return SYMPY_UNARY[op](tree_to_sympy(tree[1]))
    return SYMPY_BINARY[op](tree_to_sympy(tree[1]), tree_to_sympy(tree[2]))


def score_candidate(tree, X, y, y_std):
    """Normalized RMSE of the best multiplicative fit c * f(X) to y.

    Fitting a single scale constant makes the score invariant to overall
    physical constants (e.g. 2*pi, G), so the search only has to find the
    functional form. Returns np.inf for candidates that are invalid anywhere
    on the sampled domain.
    """
    with np.errstate(all="ignore"):
        f = eval_tree(tree, X)
        if not np.all(np.isfinite(f)):
            return np.inf, 0.0
        denom = float(np.dot(f, f))
        if denom < 1e-30:
            return np.inf, 0.0
        c = float(np.dot(f, y)) / denom
        resid = c * f - y
        nrmse = float(np.sqrt(np.mean(resid ** 2)) / y_std)
    if not np.isfinite(nrmse):
        return np.inf, 0.0
    return nrmse, c


def verify_candidate(tree, truth_expr):
    """Symbolically check that the candidate equals the truth up to a constant
    factor: simplify(truth / candidate) must reduce to a finite nonzero constant."""
    if tree_complexity(tree) > 2 ** (MAX_DEPTH + 1):
        return 0
    try:
        cand = tree_to_sympy(tree)
        if cand == 0:
            return 0
        ratio = sp.simplify(truth_expr / cand)
        if ratio.free_symbols:
            return 0
        if ratio == 0 or ratio.has(sp.zoo, sp.nan, sp.oo, -sp.oo):
            return 0
        return 1
    except Exception:
        return 0


def run_search(benchmark, bench_index, seed_offset):
    spec = BENCHMARKS[benchmark]
    n_vars = len(spec["ranges"])
    rng = np.random.default_rng([bench_index, seed_offset])

    X = np.column_stack(
        [rng.uniform(lo, hi, NUM_SAMPLES) for (lo, hi) in spec["ranges"]]
    )
    truth_fn = sp.lambdify(ALL_VARS[:n_vars], spec["expr"], "numpy")
    y = np.asarray(truth_fn(*[X[:, i] for i in range(n_vars)]), dtype=float)
    y_std = float(np.std(y))
    if y_std < 1e-12:
        raise ValueError(
            f"{benchmark}: target is (near-)constant on the sampled domain; "
            "NRMSE is undefined for constant targets"
        )

    history = []
    best_tree, best_nrmse = None, np.inf
    evals_to_solve = EVALS_PER_SEED
    curve = []
    for i in range(EVALS_PER_SEED):
        tree = propose_candidate(rng, n_vars, history)
        nrmse, _ = score_candidate(tree, X, y, y_std)
        history.append((tree, nrmse))
        if nrmse < best_nrmse:
            best_nrmse, best_tree = nrmse, tree
            if nrmse < SOLVE_TOL and evals_to_solve == EVALS_PER_SEED:
                evals_to_solve = i + 1
        if (i + 1) % RECORD_EVERY == 0:
            curve.append(
                {"eval": i + 1, "best_nrmse": float(min(best_nrmse, NRMSE_CLIP))}
            )

    solved = int(best_nrmse < SOLVE_TOL)
    verified = verify_candidate(best_tree, spec["expr"]) if solved else 0
    best_expr = str(tree_to_sympy(best_tree)) if best_tree is not None else "none"

    final_info = {
        "solved": solved,
        "verified": verified,
        "best_nrmse": float(min(best_nrmse, NRMSE_CLIP)),
        "evals_to_solve": evals_to_solve,
        "best_complexity": tree_complexity(best_tree) if best_tree is not None else 0,
    }
    print(f"  {benchmark} seed {seed_offset}: {final_info} best_expr={best_expr}")
    return final_info, curve, best_expr


parser = argparse.ArgumentParser(description="Run experiment")
parser.add_argument("--out_dir", type=str, default="run_0", help="Output directory")
args = parser.parse_args()

if __name__ == "__main__":
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    all_results = {}
    final_infos = {}
    per_seed_aggregate = []
    for bench_index, benchmark in enumerate(BENCHMARKS):
        print(f"Searching for {benchmark}")
        final_info_list = []
        for seed_offset in range(NUM_SEEDS):
            final_info, curve, best_expr = run_search(
                benchmark, bench_index, seed_offset
            )
            all_results[f"{benchmark}_{seed_offset}_final_info"] = final_info
            all_results[f"{benchmark}_{seed_offset}_history"] = curve
            all_results[f"{benchmark}_{seed_offset}_best_expr"] = best_expr
            final_info_list.append(final_info)
        final_info_dict = {
            k: [d[k] for d in final_info_list] for k in final_info_list[0].keys()
        }
        means = {f"{k}_mean": float(np.mean(v)) for k, v in final_info_dict.items()}
        # np.std(v) / sqrt(n) is the standard error of the mean; note the
        # reference templates ship std/n here, which understates uncertainty.
        stderrs = {
            f"{k}_stderr": float(np.std(v) / np.sqrt(len(v)))
            for k, v in final_info_dict.items()
        }
        final_infos[benchmark] = {
            "means": means,
            "stderrs": stderrs,
            "final_info_dict": final_info_dict,
        }

    for seed_offset in range(NUM_SEEDS):
        seed_infos = [
            all_results[f"{b}_{seed_offset}_final_info"] for b in BENCHMARKS
        ]
        per_seed_aggregate.append(
            {
                "solve_rate": float(np.mean([d["solved"] for d in seed_infos])),
                "verify_rate": float(np.mean([d["verified"] for d in seed_infos])),
                "mean_best_nrmse": float(
                    np.mean([d["best_nrmse"] for d in seed_infos])
                ),
            }
        )
    agg_dict = {
        k: [d[k] for d in per_seed_aggregate] for k in per_seed_aggregate[0].keys()
    }
    final_infos["aggregate"] = {
        "means": {f"{k}_mean": float(np.mean(v)) for k, v in agg_dict.items()},
        "stderrs": {
            f"{k}_stderr": float(np.std(v) / np.sqrt(len(v)))
            for k, v in agg_dict.items()
        },
        "final_info_dict": agg_dict,
    }

    with open(os.path.join(out_dir, "final_info.json"), "w") as f:
        json.dump(final_infos, f, indent=2)

    with open(os.path.join(out_dir, "all_results.npy"), "wb") as f:
        np.save(f, all_results)

    print(json.dumps(final_infos["aggregate"]["means"], indent=2))
