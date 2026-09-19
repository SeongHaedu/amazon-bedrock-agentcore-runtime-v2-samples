#!/usr/bin/env python3
"""Render a single-panel chart comparing the V1 / V2 pre-entrypoint latency distributions.

Pre-entrypoint is the interval from dispatched_at to agent-bench's [ENTRYPOINT_REACHED]. It is
not microVM boot time: network, auth, routing, environment preparation and HTTP dispatch are
all mixed into it. The floor measured on a reused session (the second invoke onwards) was
156 - 159 ms in our own runs. V1 carries module-scope initialization here; V2 does not.

The interval is narrowed to this segment because most of the end-to-end variance comes from
the LLM call, which drowns out the effect of platformVersion.

Encoding: the color family is the deployment mode (CodeZip = warm, Container = cool) and the
brighter member of each family is V2. Every series is drawn as a solid line. The two bright
colors have less than 3:1 contrast against a light background, so each curve is labeled
directly and the fills differ in strength as well.

usage:
  python benchmark/plot_preentry.py [suffix] [subtitle-note]

  suffix          Reads results/breakdown_{suffix}.json written by build_breakdown.py.
                  Defaults to open.
  subtitle-note   Description of the measurement conditions for the subtitle. A default is
                  used when omitted.

output: benchmark/images/coldstart_distribution_preentry_{suffix}.png
"""
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from scipy.stats import gaussian_kde  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from common import RESULTS_DIR  # noqa: E402

SUFFIX = sys.argv[1] if len(sys.argv) > 1 else "open"
SUBTITLE_NOTE = (
    sys.argv[2] if len(sys.argv) > 2
    else "N=100 per series (5 TPS x 20 s sustained open loop, streaming agent); raw samples as tick marks."
)

DATA = RESULTS_DIR / f"breakdown_{SUFFIX}.json"
OUT = Path(__file__).resolve().parent / "images" / f"coldstart_distribution_preentry_{SUFFIX}.png"

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#dcdbd5"

# Varying only lightness within one family does not separate the series for normal vision, so
# the hues are shifted apart as well. CodeZip = warm, Container = cool, brighter = V2.
STYLE = {
    "CodeZip V1": "#eb6834",
    "CodeZip V2": "#d9a600",
    "Container V1": "#2a78d6",
    "Container V2": "#2fb5d9",
}
FILL_ALPHA = {"V1": 0.07, "V2": 0.13}
PANEL_KEY = "startup_ms"
PANEL_TITLE = "Pre-entrypoint — dispatch to agent entrypoint"
PANEL_NOTE = (
    "Network, auth, routing, environment preparation, dispatch. Not microVM boot alone;\n"
    "floor 156-159 ms on a reused session. V1 also carries module-scope init here; V2 does not."
)

BANDWIDTH_S = 0.25  # One absolute kernel width for every series; varying it hides shape differences.
BIN_S = 0.5  # Converts height into "requests per BIN_S"
X_MIN, X_MAX = 0.0, 10.5

if not DATA.exists():
    raise SystemExit(f"{DATA} does not exist. Run build_breakdown.py first.")

data = json.loads(DATA.read_text())
# Fix the series order to the order of the color definitions rather than dict insertion order.
data = {k: data[k] for k in STYLE if k in data}
if not data:
    raise SystemExit(f"{DATA} holds no known series. Series names must be one of {list(STYLE)}.")

grid = np.linspace(X_MIN, X_MAX, 1500)

fig, ax = plt.subplots(1, 1, figsize=(10.0, 6.4), dpi=200)
fig.patch.set_facecolor(SURFACE)

curves = {}
for label, series in data.items():
    values = np.array(series[PANEL_KEY]) / 1000.0
    norm_n = len(values)
    kde = gaussian_kde(values, bw_method=BANDWIDTH_S / values.std(ddof=1))
    curves[label] = (values, kde(grid) * norm_n * BIN_S)
top = max(c.max() for _, c in curves.values()) * 1.46
rug_step = top * 0.027

LABEL_LANES = (top * 0.895, top * 0.805)
LABEL_MIN_GAP = 2.6  # Minimum x distance (seconds) kept between labels in one lane


def place_labels(ax, peaks):
    """Place the labels in lanes above the curves and draw a leader line to each peak.

    Two lanes are used alternately in ascending x order, and within a lane each label is
    pushed right to keep the minimum gap. Series with nearby peaks do not collide.
    """
    lane_last_x = {0: -1e9, 1: -1e9}
    for i, (label, color, x, y) in enumerate(sorted(peaks, key=lambda p: p[2])):
        lane = i % 2
        label_x = max(x, lane_last_x[lane] + LABEL_MIN_GAP)
        label_x = min(label_x, X_MAX - 0.9)
        lane_last_x[lane] = label_x
        label_y = LABEL_LANES[lane]
        ax.plot([x, label_x], [y + top * 0.012, label_y - top * 0.012],
                color=color, lw=0.9, alpha=0.5, zorder=3)
        ax.annotate(label, xy=(label_x, label_y), ha="center", va="bottom", color=color,
                    fontsize=10.5, fontweight="bold", zorder=5)


ax.set_facecolor(SURFACE)
# Draw all the fills first. Keeping them in a layer below the lines means no series line is
# hidden where the regions overlap.
for label in data:
    _, curve = curves[label]
    alpha = FILL_ALPHA["V2" if "V2" in label else "V1"]
    ax.fill_between(grid, curve, color=STYLE[label], alpha=alpha, lw=0, zorder=2)

peaks = []
for i, label in enumerate(data):
    color = STYLE[label]
    values, curve = curves[label]
    ax.plot(grid, curve, color=color, lw=2.2, solid_capstyle="round",
            zorder=4 if "V2" in label else 3, label=label)
    y = -rug_step * (i + 1)
    ax.plot(values, np.full_like(values, y), "|", color=color, ms=6, mew=1.1,
            alpha=0.75, zorder=3)
    peaks.append((label, color, grid[int(np.argmax(curve))], curve.max()))
place_labels(ax, peaks)

ax.set_xlim(X_MIN, X_MAX)
ax.set_ylim(-rug_step * (len(data) + 1), top)
ax.axhline(0, color=GRID, lw=0.8, zorder=1)
ax.set_title(PANEL_TITLE, color=INK, fontsize=12, fontweight="bold", loc="left", pad=44)
ax.text(0, 1.012, PANEL_NOTE, transform=ax.transAxes, color=INK_2,
        fontsize=9, va="bottom", linespacing=1.45)
ax.set_xlabel("Latency (s)", color=INK_2, fontsize=10.5, labelpad=8)
ax.set_xticks(np.arange(0, int(X_MAX) + 1, 1))
ax.tick_params(axis="x", colors=INK_2, labelsize=10, length=0, pad=6)
ax.tick_params(axis="y", colors=INK_2, labelsize=9.5, length=0, pad=4)
ax.grid(axis="both", color=GRID, lw=0.8, zorder=1)
ax.set_axisbelow(True)
for side in ("top", "right", "left", "bottom"):
    ax.spines[side].set_visible(False)

ax.set_ylabel("Requests", color=INK_2, fontsize=10.5, labelpad=8)
tick_step = 5 if top <= 40 else 10
ax.set_yticks(np.arange(0, top, tick_step))

legend = ax.legend(loc="upper right", frameon=False, fontsize=10.5, handlelength=2.0,
                   labelspacing=0.5, borderaxespad=0.6)
for text in legend.get_texts():
    text.set_color(INK_2)

fig.suptitle("AgentCore Runtime cold start: platformVersion V1 vs V2",
             color=INK, fontsize=15, fontweight="bold", x=0.008, ha="left", y=0.99)
fig.text(0.008, 0.951,
         "Same artifact, region, role and environment variables; only platformVersion differs.\n"
         "Warm tones = CodeZip, cool = Container, brighter = V2.\n"
         f"{SUBTITLE_NOTE}",
         color=INK_2, fontsize=9.5, va="top", linespacing=1.5)

fig.tight_layout(rect=(0, 0, 1, 0.878))
OUT.parent.mkdir(parents=True, exist_ok=True)
fig.savefig(OUT, facecolor=SURFACE)
print(f"wrote {OUT}")
for label, series in data.items():
    s = np.array(series["startup_ms"]) / 1000
    print(f"  {label:14s} n={len(s):3d}  pre-entrypoint p50={np.median(s):.2f}s sd={s.std(ddof=1):.2f}s")
