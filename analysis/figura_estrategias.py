"""Figura: exactitud balanceada por tramo de kappa humano, una línea por estrategia,
un panel por modelo; fila 1 análisis principal (faltantes como error), fila 2 solo
salidas válidas. Lee out/acuerdo_por_estrategia.csv."""
import sys
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

OUT = Path(sys.argv[1]); DEST = Path(sys.argv[2])
r = pd.read_csv(OUT / "acuerdo_por_estrategia.csv")

INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e4e3de"
COL = {"A": "#2a78d6", "B": "#eb6834", "C": "#1baf7a"}          # slots 1-3, validados
MRK = {"A": "o", "B": "s", "C": "^"}
NOM = {"A": "A: consulta única", "B": "B: harness", "C": "C: harness + modelo del caso"}
TRAMOS = ["kappa>=0.8", "0.6<=kappa<0.8", "kappa<0.6"]
XLAB = ["κ ≥ 0,8", "0,6 ≤ κ < 0,8", "κ < 0,6"]
MODELOS = ["Gemma4", "Qwen"]
FILAS = [("faltante_como_error", "Todas las salidas; campo faltante = error"),
         ("solo_validos", "Solo salidas que pasan el validador")]

plt.rcParams.update({"font.family": "serif", "font.serif": ["Times New Roman", "Times", "DejaVu Serif"],
                     "font.size": 10, "axes.edgecolor": MUTED, "axes.labelcolor": INK2,
                     "xtick.color": INK2, "ytick.color": INK2})
coma = FuncFormatter(lambda v, _: f"{v:.2f}".replace(".", ","))

fig, axes = plt.subplots(2, 2, figsize=(5.6, 5.0), sharex=True)
for i, (var, titulo_fila) in enumerate(FILAS):
    sub = r[(r.Variante == var) & (r.Alcance.isin(TRAMOS))]
    ymin = max(0.5, (sub.exactitud_balanceada.min() // 0.05) * 0.05 - 0.02)
    for j, mod in enumerate(MODELOS):
        ax = axes[i, j]
        ends = []
        for a in "ABC":
            s = sub[(sub.Modelo == mod) & (sub.Estrategia == a)].set_index("Alcance").reindex(TRAMOS)
            y = s.exactitud_balanceada.values
            if pd.isna(y).all():
                continue
            ax.plot(range(3), y, color=COL[a], lw=2, marker=MRK[a], ms=6.5, mec="white", mew=1.2, zorder=3)
            ends.append([y[-1], a])
        # etiquetas directas al final de cada línea, separadas si se superponen
        ends.sort()
        gap = 0.035 * (1.0 - ymin) / 0.5
        for k in range(1, len(ends)):
            if ends[k][0] - ends[k - 1][0] < gap:
                ends[k][0] = ends[k - 1][0] + gap
        for yv, a in ends:
            ax.text(2.12, yv, a, color=INK, fontsize=9, va="center", fontweight="bold")
        ax.set_ylim(ymin, 1.0)
        ax.set_xlim(-0.2, 2.35)
        ax.yaxis.set_major_formatter(coma)
        ax.grid(axis="y", color=GRID, lw=0.8); ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.set_xticks(range(3)); ax.set_xticklabels(XLAB, fontsize=8.5)
        if i == 0:
            ax.set_title({"Gemma4": "Gemma"}.get(mod, mod), fontsize=10.5, color=INK)
        if j == 0:
            ax.set_ylabel("Exactitud balanceada", fontsize=9.5)
    axes[i, 0].annotate(titulo_fila, xy=(0, 1.02), xycoords="axes fraction", fontsize=9, color=INK2,
                        ha="left", va="bottom", style="italic") if i == 1 else None
fig.text(0.5, 0.005, "Tramo de acuerdo entre anotadores (kappa de Fleiss humano del campo)", ha="center", fontsize=9.5, color=INK2)
handles = [plt.Line2D([], [], color=COL[a], marker=MRK[a], lw=2, ms=6.5, mec="white", label=NOM[a]) for a in "ABC"]
fig.legend(handles=handles, loc="upper center", ncol=3, frameon=False, fontsize=9, bbox_to_anchor=(0.5, 1.0))
fig.text(0.01, 0.905, FILAS[0][1], fontsize=9, color=INK2, style="italic")
fig.tight_layout(rect=(0, 0.03, 1, 0.9))
fig.savefig(DEST, dpi=300, facecolor="white")
print("ok", DEST)
