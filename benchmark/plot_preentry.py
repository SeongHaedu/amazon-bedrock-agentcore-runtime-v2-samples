#!/usr/bin/env python3
"""V1 / V2 の pre-entrypoint レイテンシー分布を 1 面で比較する図を生成する。

pre-entrypoint は dispatched_at から agent-bench の [ENTRYPOINT_REACHED] までの区間です。
microVM の起動時間そのものではありません。ネットワーク・認証・ルーティング・実行環境の準備・
HTTP ディスパッチが混在します。同一セッションを再利用した 2 回目以降で測った下限は 156 〜 159 ms
でした (手元の検証)。V1 にはここにモジュールスコープの初期化が含まれ、V2 には含まれません。

この区間に絞る理由は、end-to-end のばらつきの大半が LLM 呼び出し側に由来し、platformVersion の
効果が読めなくなるためです。

符号化: 色の系統がデプロイ方式 (CodeZip = 暖色 / Container = 寒色)、系統内の明るい方が V2 です。
線種は全系列で実線です。明るい 2 色は明るい背景に対するコントラストが 3:1 未満のため、各曲線に
直接ラベルを置き、塗りの濃さにも差をつけています。

usage:
  python benchmark/plot_preentry.py [suffix] [subtitle-note]

  suffix          build_breakdown.py が書いた results/breakdown_{suffix}.json を読む。既定は open。
  subtitle-note   図の副題に入れる計測条件の説明。省略時は既定の文言を使う。

出力: benchmark/images/coldstart_distribution_preentry_{suffix}.png
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

# 同系統で明度だけを変えると normal vision での色差が足りず判別できないため、色相もずらして
# 色差を広げている。CodeZip = 暖色、Container = 寒色、明るい方が V2 である。
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

BANDWIDTH_S = 0.25  # 全系列で同一の絶対カーネル幅にする。系列ごとに変えると形を比較できない。
BIN_S = 0.5  # 高さを「BIN_S あたりの件数」に換算する
X_MIN, X_MAX = 0.0, 10.5

if not DATA.exists():
    raise SystemExit(f"{DATA} が無い。先に build_breakdown.py を実行する。")

data = json.loads(DATA.read_text())
# 系列の並びを色定義の順に固定する。dict の挿入順に依存させない。
data = {k: data[k] for k in STYLE if k in data}
if not data:
    raise SystemExit(f"{DATA} に既知の系列が無い。系列名は {list(STYLE)} のいずれかである必要がある。")

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
LABEL_MIN_GAP = 2.6  # 同一レーン内で確保する x 方向の最小間隔 (秒)


def place_labels(ax, peaks):
    """曲線の上のレーンにラベルを置き、各ピークへ引き出し線を引く。

    x 昇順に 2 レーンを交互に使い、レーン内では最小間隔を確保して右へ押し出す。
    ピークが近接している系列でもラベルが衝突しない。
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
# 塗りを先にまとめて描く。線より下のレイヤに置くことで、重なった領域でも各系列の線が隠れない。
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
