import json
import os
import os.path as osp

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

NRMSE_FLOOR = 1e-17  # exact-zero NRMSE occurs; floor it so log plots stay visible

# LOAD FINAL RESULTS:
folders = os.listdir("./")
final_results = {}
results_info = {}
for folder in folders:
    if not (folder.startswith("run") and osp.isdir(folder)):
        continue
    try:
        with open(osp.join(folder, "final_info.json"), "r") as f:
            final_results[folder] = json.load(f)
        results_dict = np.load(
            osp.join(folder, "all_results.npy"), allow_pickle=True
        ).item()
    except FileNotFoundError as e:
        print(f"Skipping {folder}: {e}")
        continue
    benchmarks = [k for k in final_results[folder] if k != "aggregate"]
    run_info = {}
    for benchmark in benchmarks:
        curves = []
        for k in results_dict.keys():
            if k.startswith(f"{benchmark}_") and k.endswith("_history"):
                curves.append(
                    (
                        [pt["eval"] for pt in results_dict[k]],
                        [pt["best_nrmse"] for pt in results_dict[k]],
                    )
                )
        run_info[benchmark] = curves
    results_info[folder] = run_info

# CREATE LEGEND -- ADD RUNS HERE THAT WILL BE PLOTTED
labels = {
    "run_0": "Baseline",
}

runs = [r for r in labels if r in results_info]
benchmarks = sorted({b for r in runs for b in results_info[r]})
colors = plt.cm.tab10(np.linspace(0, 1, max(len(runs), 1)))

# Plot 1: convergence of best NRMSE (log scale) per benchmark, mean over seeds.
ncols = 3
nrows = max(1, int(np.ceil(len(benchmarks) / ncols)))
fig, axs = plt.subplots(
    nrows, ncols, figsize=(5 * ncols, 4 * nrows), sharex=True, squeeze=False
)
for i, benchmark in enumerate(benchmarks):
    ax = axs[i // ncols][i % ncols]
    for j, run in enumerate(runs):
        curves = results_info[run].get(benchmark, [])
        if not curves:
            continue
        # Seeds may record different numbers of points (e.g. early stopping);
        # aggregate on the common prefix.
        n_pts = min(len(c[1]) for c in curves)
        evals = curves[0][0][:n_pts]
        vals = np.maximum(
            np.array([c[1][:n_pts] for c in curves]), NRMSE_FLOOR
        )
        mean = vals.mean(axis=0)
        sem = vals.std(axis=0) / np.sqrt(len(curves))
        ax.plot(evals, mean, label=labels[run], color=colors[j])
        ax.fill_between(
            evals,
            np.maximum(mean - sem, NRMSE_FLOOR),
            mean + sem,
            color=colors[j],
            alpha=0.2,
        )
    ax.set_yscale("log")
    ax.set_title(benchmark)
    if i // ncols == nrows - 1:
        ax.set_xlabel("Candidate evaluations")
    if i % ncols == 0:
        ax.set_ylabel("Best NRMSE")
axs[0][0].legend()
fig.suptitle("Search convergence per benchmark (mean over seeds)")
fig.tight_layout()
fig.savefig("convergence.png")
plt.close(fig)

# Plot 2: solve and symbolic-verification rates per benchmark.
fig, axs = plt.subplots(1, 2, figsize=(14, 5))
x = np.arange(len(benchmarks))
width = 0.8 / max(len(runs), 1)
for j, run in enumerate(runs):
    means = final_results[run]
    solve = [
        means[b]["means"]["solved_mean"] if b in means else 0.0
        for b in benchmarks
    ]
    verify = [
        means[b]["means"]["verified_mean"] if b in means else 0.0
        for b in benchmarks
    ]
    axs[0].bar(x + j * width, solve, width, label=labels[run], color=colors[j])
    axs[1].bar(x + j * width, verify, width, label=labels[run], color=colors[j])
for ax, title in zip(axs, ["Numerically solved rate", "Symbolically verified rate"]):
    ax.set_xticks(x + 0.4 - width / 2)
    ax.set_xticklabels(benchmarks, rotation=30, ha="right")
    ax.set_ylim(0, 1.05)
    ax.set_title(title)
axs[0].legend()
fig.tight_layout()
fig.savefig("solve_rates.png")
plt.close(fig)

print("Saved convergence.png and solve_rates.png")
