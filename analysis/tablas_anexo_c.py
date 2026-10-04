"""Genera las tablas LaTeX del Anexo C del informe a partir de los CSV de esta carpeta.
Solo Gemma y Qwen (Llama-70B queda fuera de la comparación por decisión del autor:
0 salidas válidas con la consulta única). Salida: documento_final/anexos/anexo_c_{concordancia,acuerdo,sensibilidad}.tex

Uso:  python3 tablas_anexo_c.py
"""
import json, re
from pathlib import Path
import pandas as pd

D = Path(__file__).parent
OUT = D.parent.parent / "anexos" / "anexo_c_tablas.tex"
OUT.parent.mkdir(exist_ok=True)
MODELOS = [("Gemma4", "Gemma"), ("Qwen", "Qwen")]
BLOQUE = {"antecedentes": "Antecedentes", "complicaciones": "Complicaciones", "egreso": "Egreso", "falla": "Falla orgánica",
          "infecciones": "Infecciones", "ingreso": "Ingreso", "soporte": "Soporte", "calidad": "Calidad del registro",
          "hospitalizacion": "Hospitalización"}
TRAMO = {"kappa>=0.8": "$\\kappa \\geq 0{,}8$", "0.6<=kappa<0.8": "$0{,}6 \\leq \\kappa < 0{,}8$", "kappa<0.6": "$\\kappa < 0{,}6$", "todos": "Todos"}
VAR = {"faltante_como_error": "Faltante como error", "respondidas": "Solo campos respondidos", "solo_validos": "Solo salidas válidas"}
labels = json.load(open(D / "etiquetas_formulario.json"))


def esc(t):
    return str(t).replace("&", "\\&").replace("%", "\\%").replace("_", "\\_").replace("#", "\\#")


def num(v, d=3):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "--"
    s = f"{v:.{d}f}".replace(".", ",")
    return s.replace("-", "$-$", 1) if s.startswith("-") else s


def ic(lo, hi):
    return f"[{num(lo)}; {num(hi)}]"


def etiqueta(key):
    lab = labels.get(key, key.split(".")[-1].replace("_", " "))
    # contexto: último nivel intermedio cuando la etiqueta sola es ambigua (p. ej. "Agente microbiológico")
    partes = key.split(".")
    if len(partes) >= 3 and partes[-1] in ("agente", "tratamiento", "iaas", "tipo", "fecha_inicio", "fecha_fin", "otro", "otra"):
        padre = labels.get(".".join(partes[:-1]), partes[-2].replace("_", " "))
        return f"{padre}: {lab[0].lower() + lab[1:] if lab else lab}"
    return lab


L = []
w = L.append

# ---------------- C.2 Concordancia humana ----------------
res = json.load(open(D / "concordancia_humana_resumen.json"))
w("% Tabla C.1: concordancia por bloque (171 campos activos)")
w("\\begin{table}[htbp]\\centering\\small")
w("\\begin{tabular}{lrcc}\\toprule")
w("\\textbf{Bloque} & \\textbf{Campos} & \\textbf{Kappa de Fleiss} & \\textbf{AC1} \\\\ \\midrule")
for b in sorted(res["por_bloque"], key=lambda x: -x["kappa"]):
    w(f"{BLOQUE.get(b['Nivel_1'], b['Nivel_1'])} & {b['n']} & {num(b['kappa'])} & {num(b['ac1'])} \\\\")
w("\\bottomrule\\end{tabular}")
w("\\caption{Concordancia entre anotadores por bloque del formulario, al corte del 29 de septiembre de 2026: promedio del kappa de Fleiss y del AC1 de Gwet sobre los "
  f"{res['campos_activos']} campos activos (se excluyen {res['campos_siempre_falsos_excluidos']} campos que los anotadores siempre respondieron como No, con kappa indeterminado). "
  f"Base: {res['epicrisis_2_o_mas_anotadores']} epicrisis con dos o más anotaciones.}}")
w("\\label{tab:c-concordancia-bloque}\\end{table}\n")

conc = pd.read_csv(D / "concordancia_por_campo.csv")
conc = conc[conc.Grupo == "Todas (2-3 anotadores)"].set_index("Campo_especifico")
campos = pd.read_csv(D / "campos_analizados.csv").sort_values(["Bloque", "Campo_especifico"])
w("% Tabla C.2: concordancia por campo (90 campos comparados con el modelo)")
w("\\begin{longtable}{p{6.9cm}ccc}")
w("\\caption{Concordancia entre anotadores en los 90 campos comparados con los modelos: kappa de Fleiss con intervalo de confianza de 95\\% (\\textit{bootstrap}), AC1 de Gwet y porcentaje de acuerdo simple. Un kappa bajo con AC1 alto corresponde a la paradoja del kappa en campos de prevalencia extrema.}\\label{tab:c-concordancia-campo}\\\\")
w("\\toprule \\textbf{Variable} & \\textbf{Kappa [IC 95\\%]} & \\textbf{AC1} & \\textbf{Acuerdo (\\%)} \\\\ \\midrule \\endfirsthead")
w("\\toprule \\textbf{Variable} & \\textbf{Kappa [IC 95\\%]} & \\textbf{AC1} & \\textbf{Acuerdo (\\%)} \\\\ \\midrule \\endhead")
w("\\midrule \\multicolumn{4}{r}{\\small continúa en la página siguiente} \\\\ \\endfoot \\bottomrule \\endlastfoot")
for b, grp in campos.groupby("Bloque", sort=True):
    w(f"\\multicolumn{{4}}{{l}}{{\\textit{{{BLOQUE.get(b, b)}}}}} \\\\")
    for r in grp.itertuples(index=False):
        c = conc.loc[r.Campo_especifico]
        w(f"\\quad {esc(etiqueta(r.Campo_especifico))} & {num(c.Kappa_Fleiss)} {ic(c.Kappa_IC95_inf, c.Kappa_IC95_sup)} & {num(c.AC1_Gwet)} & {num(c.Porcentaje_matching_promedio, 1)} \\\\")
w("\\end{longtable}\n")

# ---------------- C.3 Acuerdo por campo y estrategia ----------------
pce = pd.read_csv(D / "acuerdo_por_campo_estrategia.csv")
w("% Tabla C.3: exactitud balanceada por campo, estrategia y modelo (variante principal)")
w("\\begin{longtable}{p{5.6cm}c|ccc|ccc}")
w("\\caption{Exactitud balanceada por campo, estrategia y modelo, en la variante principal (campo sin respuesta Sí/No contado como error), sobre los pacientes con referencia por mayoría. Columna $n$: pares con referencia.}\\label{tab:c-acuerdo-campo}\\\\")
hdr = "\\toprule & & \\multicolumn{3}{c|}{\\textbf{Gemma}} & \\multicolumn{3}{c}{\\textbf{Qwen}} \\\\ \\textbf{Variable} & $n$ & A & B & C & A & B & C \\\\ \\midrule"
w(hdr + " \\endfirsthead"); w(hdr + " \\endhead")
w("\\midrule \\multicolumn{8}{r}{\\small continúa en la página siguiente} \\\\ \\endfoot \\bottomrule \\endlastfoot")
for b, grp in campos.groupby("Bloque", sort=True):
    w(f"\\multicolumn{{8}}{{l}}{{\\textit{{{BLOQUE.get(b, b)}}}}} \\\\")
    for r in grp.itertuples(index=False):
        sub = pce[pce.Campo_especifico == r.Campo_especifico].set_index(["Modelo", "Estrategia"])
        n = int(sub.n_ref.iloc[0])
        vals = " & ".join(num(sub.loc[(m, a), "eb_principal"]) for m, _ in MODELOS for a in "ABC")
        w(f"\\quad {esc(etiqueta(r.Campo_especifico))} & {n} & {vals} \\\\")
w("\\end{longtable}\n")

loo = pd.read_csv(D / "kappa_loo_por_campo.csv")
w("La Tabla~\\ref{tab:c-loo-campo} presenta, para cada campo, el kappa al reemplazar a un anotador por el modelo.\n")
w("% Tabla C.4: kappa leave-one-out por campo")
w("\\begin{longtable}{p{5.6cm}c|ccc|ccc}")
w("\\caption{Kappa de Fleiss al reemplazar a un anotador por el modelo (\\textit{leave-one-out}, promedio de las tres posiciones), por campo, estrategia y modelo, sobre los campos que el modelo respondió. La columna $\\kappa_h$ es el kappa humano original del campo.}\\label{tab:c-loo-campo}\\\\")
hdr = "\\toprule & & \\multicolumn{3}{c|}{\\textbf{Gemma}} & \\multicolumn{3}{c}{\\textbf{Qwen}} \\\\ \\textbf{Variable} & $\\kappa_h$ & A & B & C & A & B & C \\\\ \\midrule"
w(hdr + " \\endfirsthead"); w(hdr + " \\endhead")
w("\\midrule \\multicolumn{8}{r}{\\small continúa en la página siguiente} \\\\ \\endfoot \\bottomrule \\endlastfoot")
for b, grp in campos.groupby("Bloque", sort=True):
    w(f"\\multicolumn{{8}}{{l}}{{\\textit{{{BLOQUE.get(b, b)}}}}} \\\\")
    for r in grp.itertuples(index=False):
        sub = loo[loo.Campo_especifico == r.Campo_especifico].set_index(["Modelo", "Estrategia"])
        vals = " & ".join(num(sub.loc[(m, a), "kappa_loo_promedio"]) for m, _ in MODELOS for a in "ABC")
        w(f"\\quad {esc(etiqueta(r.Campo_especifico))} & {num(r.Kappa_humano)} & {vals} \\\\")
w("\\end{longtable}\n")

blq = pd.read_csv(D / "acuerdo_por_bloque.csv")
w("La Tabla~\\ref{tab:c-acuerdo-bloque} agrega por bloque del formulario la exactitud balanceada.\n")
w("% Tabla C.5: acuerdo por bloque")
w("\\begin{table}[htbp]\\centering\\small")
w("\\begin{tabular}{lcc|ccc|ccc}\\toprule")
w(" & & & \\multicolumn{3}{c|}{\\textbf{Gemma}} & \\multicolumn{3}{c}{\\textbf{Qwen}} \\\\")
w("\\textbf{Bloque} & \\textbf{Campos} & $\\kappa_h$ & A & B & C & A & B & C \\\\ \\midrule")
for b, grp in blq.groupby("Bloque", sort=True):
    g = grp.set_index(["Modelo", "Estrategia"])
    vals = " & ".join(num(g.loc[(m, a), "exactitud_balanceada"]) for m, _ in MODELOS for a in "ABC")
    w(f"{BLOQUE.get(b, b)} & {int(grp.n_campos.iloc[0])} & {num(grp.kappa_humano_prom.iloc[0])} & {vals} \\\\")
w("\\bottomrule\\end{tabular}")
w("\\caption{Exactitud balanceada por bloque del formulario, estrategia y modelo (variante principal, 90 campos). $\\kappa_h$: kappa humano promedio de los campos del bloque.}")
w("\\label{tab:c-acuerdo-bloque}\\end{table}\n")

# ---------------- C.4 Sensibilidades ----------------
acu = pd.read_csv(D / "acuerdo_por_estrategia.csv")
w("% Tabla C.6: tres convenciones")
w("\\begin{table}[htbp]\\centering\\footnotesize")
w("\\begin{tabular}{llccc|ccc}\\toprule")
w(" & & \\multicolumn{3}{c|}{\\textbf{Exactitud balanceada}} & \\multicolumn{3}{c}{\\textbf{F1}} \\\\")
w("\\textbf{Modelo} & \\textbf{Estr.} & Faltante = error & Respondidos & Solo válidas & Faltante = error & Respondidos & Solo válidas \\\\ \\midrule")
for m, mn in MODELOS:
    for i, a in enumerate("ABC"):
        row = {v: acu[(acu.Modelo == m) & (acu.Estrategia == a) & (acu.Alcance == "todos") & (acu.Variante == v)].iloc[0] for v in VAR}
        eb = " & ".join(num(row[v].exactitud_balanceada) for v in VAR)
        f1 = " & ".join(num(row[v].f1) for v in VAR)
        w(f"{mn if i == 0 else ''} & {a} & {eb} & {f1} \\\\")
    if m != MODELOS[-1][0]:
        w("\\midrule")
w("\\bottomrule\\end{tabular}")
w("\\caption{Sensibilidad a la convención sobre los campos sin respuesta (90 campos, 44 pacientes, referencia por mayoría): la variante principal cuenta el campo sin Sí o No como error; la convención del análisis original descarta ese par; la tercera usa solo las salidas que pasan el validador.}")
w("\\label{tab:c-convenciones}\\end{table}\n")

refd = pd.read_csv(D / "acuerdo_por_referencia.csv")
w("% Tabla C.7: unanimidad frente a mayoría")
w("\\begin{table}[htbp]\\centering\\small")
w("\\begin{tabular}{llcc|cc}\\toprule")
w(" & & \\multicolumn{2}{c|}{\\textbf{Mayoría}} & \\multicolumn{2}{c}{\\textbf{Unanimidad}} \\\\")
w("\\textbf{Modelo} & \\textbf{Estr.} & EB & $n$ & EB & $n$ \\\\ \\midrule")
for m, mn in MODELOS:
    for i, a in enumerate("ABC"):
        q = refd[(refd.Modelo == m) & (refd.Estrategia == a) & (refd.Alcance == "todos") & (refd.Variante == "faltante_como_error")].set_index("Referencia")
        miles = lambda k: f"{int(k):,}".replace(",", ".")
        w(f"{mn if i == 0 else ''} & {a} & {num(q.loc['mayoria', 'exactitud_balanceada'])} & {miles(q.loc['mayoria', 'n'])} & {num(q.loc['unanimidad', 'exactitud_balanceada'])} & {miles(q.loc['unanimidad', 'n'])} \\\\")
    if m != MODELOS[-1][0]:
        w("\\midrule")
w("\\bottomrule\\end{tabular}")
w("\\caption{Sensibilidad a la definición de la referencia humana (variante principal, 90 campos): mayoría de dos o más anotadores frente a unanimidad estricta, que descarta los pares con un anotador en desacuerdo. $n$: pares con referencia.}")
w("\\label{tab:c-referencia}\\end{table}\n")

dif = pd.read_csv(D / "diferencias_pareadas_bootstrap.csv")
w("% Tabla C.8: diferencias por tramo, incluida C-A")
w("\\begin{table}[htbp]\\centering\\scriptsize")
w("\\begin{tabular}{llcccc}\\toprule")
w("\\textbf{Modelo} & \\textbf{Diferencia} & \\textbf{Todos} & " + " & ".join(TRAMO[t] for t in ("kappa>=0.8", "0.6<=kappa<0.8", "kappa<0.6")) + " \\\\ \\midrule")
for m, mn in MODELOS:
    for i, comp in enumerate(("B-A", "C-B", "C-A")):
        cells = []
        for alc in ("todos", "kappa>=0.8", "0.6<=kappa<0.8", "kappa<0.6"):
            q = dif[(dif.Modelo == m) & (dif.Comparacion == comp) & (dif.Alcance == alc) & (dif.Variante == "faltante_como_error") & (dif.Metrica == "exactitud_balanceada")].iloc[0]
            cells.append(f"{num(q.diferencia)} {ic(q.IC95_inf, q.IC95_sup)}")
        w(f"{mn if i == 0 else ''} & {comp.replace('-', '$-$')} & " + " & ".join(cells) + " \\\\")
    if m != MODELOS[-1][0]:
        w("\\midrule")
w("\\bottomrule\\end{tabular}")
w("\\caption{Diferencias pareadas de exactitud balanceada entre estrategias, por tramo de kappa humano, en la variante principal, con intervalos de confianza de 95\\% (\\textit{bootstrap} por paciente, 2.000 remuestreos). Incluye C$-$A, que no se reporta en el cuerpo.}")
w("\\label{tab:c-diferencias-tramo}\\end{table}\n")

est = json.load(open(D / "estructura_y_costo.json"))
w("% Tabla C.9: validez con reetiquetado y tokens")
w("\\begin{table}[htbp]\\centering\\footnotesize")
w("\\begin{tabular}{llccccc}\\toprule")
w("\\textbf{Modelo} & \\textbf{Estr.} & \\textbf{Válidas} & \\textbf{Tras reetiquetar} & \\textbf{Casos completos} & \\textbf{Tokens entrada} & \\textbf{Tokens salida} \\\\ \\midrule")
for m, mn in MODELOS:
    for i, a in enumerate("ABC"):
        x = est[m]["arms"][a]
        w(f"{mn if i == 0 else ''} & {a} & {x['valid']} & {x['valid_after_relabelling']} & {x['cases_all_199']} & {x['tokens_in']:,}".replace(",", ".") + f" & {x['tokens_out']:,}".replace(",", ".") + " \\\\")
    if m != MODELOS[-1][0]:
        w("\\midrule")
w("\\bottomrule\\end{tabular}")
w("\\caption{Validez de las salidas (50 casos por celda) antes y después del análisis de sensibilidad del protocolo, que cambia de no documentado a no aplicable el motivo de ausencia de los datos no booleanos vacíos bajo una variable condicionante respondida como No o de un paciente fallecido, sin cambiar valores; casos completos (las 199 variables respondidas); y tokens totales de entrada y salida por estrategia.}")
w("\\label{tab:c-validez}\\end{table}\n")

txt = "\n".join(L) + "\n"
txt = txt.replace("\\begin{longtable}", "{\\small\n\\begin{longtable}").replace("\\end{longtable}", "\\end{longtable}\n}")
partes = re.split(r"(?=% Tabla C\.3)|(?=% Tabla C\.6)", txt)
for nombre, parte in zip(("anexo_c_concordancia.tex", "anexo_c_acuerdo.tex", "anexo_c_sensibilidad.tex"), partes):
    (OUT.parent / nombre).write_text(parte, encoding="utf-8")
if OUT.exists():
    OUT.unlink()
bad = [ch for ch in "κ≥≤−×→≈—–“”" if ch in txt]
print("escritos 3 archivos en", OUT.parent, "| líneas", txt.count("\n"), "| caracteres prohibidos:", bad)
