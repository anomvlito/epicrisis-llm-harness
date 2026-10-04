"""
Extiende el análisis modelo-humano (analizar_modelos_vs_humano.py, de otro
integrante del equipo) a las tres estrategias de extracción de form199-v2.6:
A consulta única, B harness, C harness con modelo del caso; con Gemma4, Qwen y
Llama-70B. Reutiliza sin modificar sus funciones de referencia por mayoría,
matriz de confusión y kappa leave-one-out, y su filtro de campos (booleanos,
prevalencia humana >= 5 %, kappa humano calculable) y tramos de kappa.

Tres formas de tratar las respuestas (preespecificadas aquí):
- principal ("faltante_como_error"): todos los casos; si hay referencia y el
  modelo no entregó un valor booleano, cuenta como error (FN si la referencia
  es Sí, FP si es No). No premia omitir variables.
- "respondidas": convención del análisis original (se omite el par si el
  modelo no respondió). Para A con Gemma4 y Qwen reproduce sus cifras.
- "solo_validos": solo casos cuya salida pasó el validador del protocolo,
  con la convención "respondidas".

Diferencias entre estrategias: bootstrap pareado por paciente (2000
remuestreos, semilla fija) sobre la exactitud balanceada micro.
Solo escribe agregados (sin IDs ni texto).
"""
import json, glob, sys
from pathlib import Path
import numpy as np
import pandas as pd

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
import analizar_modelos_vs_humano as amh
from analizar_concordancia import dividir_jerarquia

V26 = Path(sys.argv[1])
OUT = Path(sys.argv[2]); OUT.mkdir(parents=True, exist_ok=True)
ARMS = {"A": "monolithic-b16384", "B": "harness", "C": "armc-r3"}
MODELS = ["Gemma4", "Qwen", "Llama-70B"]
KEYS = [f"{a}|{m}" for m in MODELS for a in ARMS]
RNG_SEED, N_BOOT = 20260930, 2000

# ---------- datos ----------
t1 = pd.read_csv(BASE / "matching_por_epicrisis.csv")
t2 = pd.read_csv(BASE / "concordancia_por_campo.csv")
status = json.load(open(BASE / "monoliticos_gemma_qwen.json"))["casos"]
pid2caso = {v["patient_id"]: k for k, v in status.items()}

salidas, valido = {}, {}
for a, sub in ARMS.items():
    for m in MODELS:
        k = f"{a}|{m}"
        for f in glob.glob(str(V26 / sub / m / "results" / "*.json")):
            d = json.load(open(f))
            caso = pid2caso[d["patient_id"]]
            salidas[(k, caso)] = {q: v.get("valor") for q, v in d["annotations"].items()}
            valido[(k, caso)] = bool(d["valid"])

# ---------- base de campos (idéntica al análisis original) ----------
t2_todas = t2[t2.Grupo == "Todas (2-3 anotadores)"]
criterios = sorted({c for c in t2_todas[t2_todas.Tipo_dato == "booleano"].Campo_especifico})
bool_rows = t1[t1.Tipo_dato == "booleano"].copy()
vc = ["Valor_R1", "Valor_R2", "Valor_R3"]
bool_rows["tiene_dato"] = bool_rows[vc].notna().any(axis=1)
bool_rows["tiene_true"] = (bool_rows[vc] == "True").any(axis=1)
prev = bool_rows.groupby("Campo_especifico").agg(N_con_dato=("tiene_dato", "sum"), N_con_true=("tiene_true", "sum")).reset_index()
prev["pct_true"] = 100 * prev.N_con_true / prev.N_con_dato
kh = t2_todas[t2_todas.Tipo_dato == "booleano"][["Campo_especifico", "Kappa_Fleiss"]].rename(columns={"Kappa_Fleiss": "Kappa_humano"})
base = kh.merge(prev, on="Campo_especifico", how="left")
base = base[(base.pct_true >= amh.UMBRAL_PREVALENCIA) & base.Kappa_humano.notna()].copy()
base["Tramo"] = base.Kappa_humano.apply(lambda k: "kappa>=0.8" if k >= 0.8 else ("0.6<=kappa<0.8" if k >= 0.6 else "kappa<0.6"))
base["Bloque"] = base.Campo_especifico.apply(lambda c: dividir_jerarquia(c)[0])
# Solo variables que el formulario del modelo pide (199 hojas). Los nodos agrupadores
# de la plataforma (p. ej. "antecedentes") no se piden al modelo; en la convención
# original aportan n=0, así que excluirlos no cambia las métricas micro.
MODEL_KEYS = set()
for f in glob.glob(str(V26 / ARMS["A"] / "Gemma4" / "results" / "*.json")):
    MODEL_KEYS |= set(json.load(open(f))["annotations"])
assert len(MODEL_KEYS) == 199
n_base_original = len(base)
base = base[base.Campo_especifico.isin(MODEL_KEYS)].copy()
campos = list(base.Campo_especifico)

# ---------- items por campo ----------
items = {c: [] for c in campos}
for r in bool_rows.itertuples(index=False):
    if r.Campo_especifico not in items:
        continue
    h = {p: ((getattr(r, f"Valor_{p}") == "True") if pd.notna(getattr(r, f"Valor_{p}")) else None) for p in ("R1", "R2", "R3")}
    hum = [h[p] for p in ("R1", "R2", "R3") if h[p] is not None]
    it = {"caso": r.Caso, **h, "gt_mayoria": amh.ground_truth_mayoria(hum), "gt_acuerdo_total": amh.ground_truth_acuerdo_total(hum)}
    for k in KEYS:
        s = salidas.get((k, r.Caso))
        v = s.get(r.Campo_especifico) if s is not None else None
        it[k] = v if isinstance(v, bool) else None
        it[k + "#resp"] = s is not None  # el caso existe para esa estrategia
        it[k + "#valido"] = valido.get((k, r.Caso), False)
    items[r.Campo_especifico].append(it)


def confusion(its, k, variante, gt_key="gt_mayoria"):
    tp = fp = fn = tn = 0
    for it in its:
        gt = it[gt_key]
        if gt is None:
            continue
        pred = it[k]
        if variante == "solo_validos" and not it[k + "#valido"]:
            continue
        if pred is None:
            if variante != "faltante_como_error":
                continue
            pred = not gt  # error
        if pred and gt: tp += 1
        elif pred and not gt: fp += 1
        elif not pred and gt: fn += 1
        else: tn += 1
    return np.array([tp, fp, fn, tn])


def metr(c):
    tp, fp, fn, tn = c
    m = amh.metricas_desde_confusion(int(tp), int(fp), int(fn), int(tn))
    return {"n": m["n"], "f1": m["f1"], "sensibilidad": m["sensibilidad"], "especificidad": m["especificidad"], "exactitud_balanceada": m["auc"]}


def cobertura(its, k):
    tot = sum(1 for it in its if it["gt_mayoria"] is not None)
    resp = sum(1 for it in its if it["gt_mayoria"] is not None and it[k] is not None)
    return tot, resp


# ---------- 1) resumen por estrategia x modelo x alcance x variante ----------
alcances = {"todos": base, "kappa>=0.8": base[base.Tramo == "kappa>=0.8"],
            "0.6<=kappa<0.8": base[base.Tramo == "0.6<=kappa<0.8"], "kappa<0.6": base[base.Tramo == "kappa<0.6"]}
filas = []
for alc, sub in alcances.items():
    cs = list(sub.Campo_especifico)
    for k in KEYS:
        a, m = k.split("|")
        tot = resp = 0
        for c in cs:
            t, r_ = cobertura(items[c], k); tot += t; resp += r_
        for var in ("faltante_como_error", "respondidas", "solo_validos"):
            conf = sum((confusion(items[c], k, var) for c in cs), np.zeros(4, int))
            filas.append({"Alcance": alc, "n_campos": len(cs), "Estrategia": a, "Modelo": m, "Variante": var,
                          "tp": int(conf[0]), "fp": int(conf[1]), "fn": int(conf[2]), "tn": int(conf[3]), **metr(conf),
                          "cobertura_pct": round(100 * resp / tot, 1) if tot else np.nan})
resumen = pd.DataFrame(filas)
resumen.to_csv(OUT / "acuerdo_por_estrategia.csv", index=False)

# ---------- 2) kappa LOO por campo (convención original) y promedio por tramo ----------
loo = []
for row in base.itertuples(index=False):
    for k in KEYS:
        a, m = k.split("|")
        r = amh.kappa_loo_resumen(items[row.Campo_especifico], k)
        loo.append({"Campo_especifico": row.Campo_especifico, "Bloque": row.Bloque, "Tramo": row.Tramo,
                    "Kappa_humano": row.Kappa_humano, "Estrategia": a, "Modelo": m, "kappa_loo_promedio": r["kappa_loo_promedio"]})
loo = pd.DataFrame(loo)
loo.to_csv(OUT / "kappa_loo_por_campo.csv", index=False)
loo_tramo = (loo.groupby(["Modelo", "Estrategia", "Tramo"]).agg(kappa_humano_prom=("Kappa_humano", "mean"),
             kappa_loo_prom=("kappa_loo_promedio", "mean"), n_campos=("Campo_especifico", "count")).round(3).reset_index())
loo_tramo.to_csv(OUT / "kappa_loo_por_tramo.csv", index=False)

# ---------- 3) bootstrap pareado por paciente ----------
casos = sorted({it["caso"] for c in campos for it in items[c] if it["gt_mayoria"] is not None})
idx = {c: i for i, c in enumerate(casos)}
# matriz caso x (k) x 4 con la confusión por paciente, variante principal, 109 campos y por tramo
def por_paciente(cs, k, var):
    M = np.zeros((len(casos), 4), int)
    for c in cs:
        for it in items[c]:
            if it["gt_mayoria"] is None:
                continue
            M[idx[it["caso"]]] += confusion([it], k, var)
    return M

def ba(conf):
    tp, fp, fn, tn = conf
    s = tp / (tp + fn) if tp + fn else np.nan
    e = tn / (tn + fp) if tn + fp else np.nan
    return np.nanmean([s, e])

def f1(conf):
    tp, fp, fn, tn = conf
    return 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else np.nan

rng = np.random.default_rng(RNG_SEED)
boot_idx = rng.integers(0, len(casos), size=(N_BOOT, len(casos)))
dif = []
for alc in ("todos", "kappa>=0.8", "0.6<=kappa<0.8", "kappa<0.6"):
    cs = list(alcances[alc].Campo_especifico)
    for var in ("faltante_como_error", "solo_validos"):
        for m in MODELS:
            P = {a: por_paciente(cs, f"{a}|{m}", var) for a in ARMS}
            for x, y in (("B", "A"), ("C", "B"), ("C", "A")):
                for nombre, fn_ in (("exactitud_balanceada", ba), ("f1", f1)):
                    obs = fn_(P[x].sum(0)) - fn_(P[y].sum(0))
                    bs = np.array([fn_(P[x][b].sum(0)) - fn_(P[y][b].sum(0)) for b in boot_idx])
                    lo, hi = np.nanpercentile(bs, [2.5, 97.5])
                    dif.append({"Alcance": alc, "Variante": var, "Modelo": m, "Comparacion": f"{x}-{y}", "Metrica": nombre,
                                "diferencia": round(obs, 3), "IC95_inf": round(lo, 3), "IC95_sup": round(hi, 3),
                                "n_pacientes": len(casos)})
pd.DataFrame(dif).to_csv(OUT / "diferencias_pareadas_bootstrap.csv", index=False)

# ---------- 4) por bloque del formulario (variante principal, 109 campos) ----------
bl = []
for b, sub in base.groupby("Bloque"):
    cs = list(sub.Campo_especifico)
    for k in KEYS:
        a, m = k.split("|")
        conf = sum((confusion(items[c], k, "faltante_como_error") for c in cs), np.zeros(4, int))
        bl.append({"Bloque": b, "n_campos": len(cs), "kappa_humano_prom": round(sub.Kappa_humano.mean(), 3),
                   "Estrategia": a, "Modelo": m, **metr(conf)})
pd.DataFrame(bl).to_csv(OUT / "acuerdo_por_bloque.csv", index=False)

# ---------- 5) por campo: acuerdo por estrategia x modelo (para el anexo) ----------
pc = []
for row in base.itertuples(index=False):
    its = items[row.Campo_especifico]
    for k in KEYS:
        a, m = k.split("|")
        conf = confusion(its, k, "faltante_como_error"); confv = confusion(its, k, "solo_validos")
        tot, resp = cobertura(its, k)
        pc.append({"Campo_especifico": row.Campo_especifico, "Bloque": row.Bloque, "Tramo": row.Tramo, "Kappa_humano": row.Kappa_humano,
                   "Estrategia": a, "Modelo": m, "n_ref": tot, "cobertura_pct": round(100 * resp / tot, 1) if tot else np.nan,
                   "eb_principal": metr(conf)["exactitud_balanceada"], "f1_principal": metr(conf)["f1"],
                   "eb_solo_validos": metr(confv)["exactitud_balanceada"], "n_solo_validos": int(confv.sum())})
pd.DataFrame(pc).to_csv(OUT / "acuerdo_por_campo_estrategia.csv", index=False)

# ---------- 6) sensibilidad: referencia por unanimidad frente a mayoría ----------
ref = []
for alc, sub in alcances.items():
    cs = list(sub.Campo_especifico)
    for k in KEYS:
        a, m = k.split("|")
        for gt_key, nombre in (("gt_mayoria", "mayoria"), ("gt_acuerdo_total", "unanimidad")):
            for var in ("faltante_como_error", "respondidas"):
                conf = sum((confusion(items[c], k, var, gt_key) for c in cs), np.zeros(4, int))
                ref.append({"Alcance": alc, "Referencia": nombre, "Variante": var, "Estrategia": a, "Modelo": m, **metr(conf)})
pd.DataFrame(ref).to_csv(OUT / "acuerdo_por_referencia.csv", index=False)

base[["Campo_especifico", "Bloque", "Tramo", "Kappa_humano", "pct_true"]].round(3).to_csv(OUT / "campos_analizados.csv", index=False)
json.dump({"n_campos_filtro_original": n_base_original, "n_campos": len(campos), "tramos": base.Tramo.value_counts().to_dict(), "n_pacientes_con_referencia": len(casos),
           "bootstrap": {"n": N_BOOT, "semilla": RNG_SEED}}, open(OUT / "meta.json", "w"), indent=1)
print("campos", len(campos), base.Tramo.value_counts().to_dict(), "pacientes", len(casos))
