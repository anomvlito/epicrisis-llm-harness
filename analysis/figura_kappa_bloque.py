"""Figura: kappa de Fleiss humano promedio por bloque del formulario (corte 29-09),
sobre los campos activos con kappa calculable. Lee concordancia_humana_resumen.json."""
import json, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
d = json.load(open(sys.argv[1]))["por_bloque"]
d = sorted(d, key=lambda x: x["kappa"])
INK, INK2, MUTED, GRID, AZUL = "#0b0b0b", "#52514e", "#8a8984", "#e4e3de", "#2a78d6"
NOM = {"antecedentes": "Antecedentes", "calidad": "Calidad del registro", "complicaciones": "Complicaciones", "egreso": "Egreso",
       "falla": "Falla orgánica", "infecciones": "Infecciones", "ingreso": "Ingreso", "soporte": "Soporte"}
plt.rcParams.update({"font.family": "serif", "font.serif": ["Times New Roman", "Times", "DejaVu Serif"], "font.size": 10,
                     "axes.edgecolor": MUTED, "xtick.color": INK2, "ytick.color": INK})
coma = lambda v: f"{v:.2f}".replace(".", ",")
fig, ax = plt.subplots(figsize=(6.0, 3.1))
y = range(len(d))
ax.barh(list(y), [x["kappa"] for x in d], height=0.62, color=AZUL, zorder=3)
for i, x in enumerate(d):
    ax.text(x["kappa"] + 0.012, i, f'{coma(x["kappa"])}  (n = {x["n"]})', va="center", fontsize=9, color=INK, zorder=4,
            bbox=dict(facecolor="white", edgecolor="none", pad=0.6))
ax.axvline(0.8, color=INK2, lw=1, ls=(0, (3, 2)), zorder=2)
ax.text(0.8, len(d) - 0.35, "0,8", ha="center", va="bottom", fontsize=8.5, color=INK2)
ax.set_yticks(list(y)); ax.set_yticklabels([NOM.get(x["Nivel_1"], x["Nivel_1"]) for x in d])
ax.set_xlim(0, 1.08); ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.1f}".replace(".", ",")))
ax.set_xlabel("Kappa de Fleiss promedio entre anotadores", color=INK2)
ax.grid(axis="x", color=GRID, lw=0.8); ax.set_axisbelow(True)
for sp in ("top", "right"): ax.spines[sp].set_visible(False)
fig.tight_layout(); fig.savefig(sys.argv[2], dpi=300, facecolor="white"); print("ok")
