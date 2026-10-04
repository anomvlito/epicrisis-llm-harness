"""Comprueba que las Tablas 2 a 7 del cuerpo de informe_titulo.tex coinciden, cifra por cifra, con los
agregados versionados en esta carpeta (concordancia_humana_resumen.json, estructura_y_costo.json,
acuerdo_por_estrategia.csv, acuerdo_por_bloque.csv y diferencias_pareadas_bootstrap.csv).

Uso:  python3 comprobar_tablas_cuerpo.py      (termina con código 1 si alguna tabla no coincide)
"""
import json, re, sys
from pathlib import Path
import pandas as pd

D = Path(__file__).parent
TEX = (D.parent.parent / "informe_titulo.tex").read_text(encoding="utf-8")
MODELOS = ["Gemma4", "Qwen"]


def entorno(label):
    i = TEX.index("\\label{%s}" % label)
    a = TEX.rindex("\\begin{table}", 0, i)
    b = TEX.index("\\end{table}", i)
    return TEX[a:b]


def numeros(linea):
    linea = linea.replace("$-$", "-").replace("{,}", "")
    salida = []
    for m in re.finditer(r"-?\d+(?:[.,]\d+)*", linea):
        t = m.group(0)
        if "," in t:
            salida.append(float(t.replace(".", "").replace(",", ".")))
        elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
            salida.append(float(t.replace(".", "")))
        else:
            salida.append(float(t))
    return salida


def filas(tabular):
    rs = []
    for l in tabular.split("\n"):
        l = l.strip()
        if l.endswith("\\\\") and "&" in l and re.search(r"\d", l) and not l.startswith(("\\textbf", " & &", "& &", "\\multicolumn")):
            rs.append(numeros(re.sub(r"\\textbf\{[^}]*\}", "", l.rstrip("\\").strip())))
    return rs


def tabulares(e):
    return [t.split("\\end{tabular}")[0] for t in e.split("\\begin{tabular}")[1:]]


def comparar(nombre, texto, datos, tol=0.0006):
    malas = []
    if len(texto) != len(datos):
        malas.append(f"filas {len(texto)} frente a {len(datos)}")
    for i, (g, e) in enumerate(zip(texto, datos)):
        gg = g[-len(e):] if len(g) >= len(e) else g
        if len(gg) != len(e) or any(abs(x - y) > tol for x, y in zip(gg, e)):
            malas.append(f"fila {i}: texto {g} frente a datos {e}")
    print(("OK    " if not malas else "FALLA ") + nombre, f"({len(datos)} filas)")
    for m in malas[:6]:
        print("      ", m)
    return not malas


res = json.load(open(D / "concordancia_humana_resumen.json"))
est = json.load(open(D / "estructura_y_costo.json"))
ac = pd.read_csv(D / "acuerdo_por_estrategia.csv")
ab = pd.read_csv(D / "acuerdo_por_bloque.csv")
df = pd.read_csv(D / "diferencias_pareadas_bootstrap.csv")


def g(alc, m, a, var, col):
    r = ac[(ac.Alcance == alc) & (ac.Modelo == m) & (ac.Estrategia == a) & (ac.Variante == var)]
    return float(r[col].iloc[0])


ok = True
e2 = [[b["n"], b["kappa"], b["ac1"]] for b in sorted(res["por_bloque"], key=lambda x: -x["kappa"])]
ok &= comparar("Tabla 2 concordancia por bloque", filas(tabulares(entorno("tab:concordancia-bloque"))[0]), e2)
e3 = []
for m in MODELOS:
    for a in "ABC":
        x = est[m]["arms"][a]
        e3.append([x["valid"], x["final_error_categories"].get("relation_negative_parent", 0),
                   round(100 * x["fields_answered"] / x["fields_expected"], 1), x["calls"], round(x["gpu_wall_s"] / 50 / 60, 1)])
ok &= comparar("Tabla 3 validez y costo", filas(tabulares(entorno("tab:estructura"))[0]), e3, tol=0.051)
e4 = [[g("todos", m, a, "faltante_como_error", "exactitud_balanceada"), g("todos", m, a, "faltante_como_error", "f1"),
       g("todos", m, a, "faltante_como_error", "cobertura_pct"), g("todos", m, a, "solo_validos", "exactitud_balanceada"),
       g("todos", m, a, "solo_validos", "n")] for m in MODELOS for a in "ABC"]
tb = tabulares(entorno("tab:acuerdo"))
ok &= comparar("Tabla 4 acuerdo", filas(tb[0]), e4)
e5 = []
for m in MODELOS:
    for c in ("B-A", "C-B"):
        fila = []
        for var in ("faltante_como_error", "solo_validos"):
            r = df[(df.Alcance == "todos") & (df.Variante == var) & (df.Modelo == m) & (df.Comparacion == c) & (df.Metrica == "exactitud_balanceada")].iloc[0]
            fila += [r.diferencia, r.IC95_inf, r.IC95_sup]
        e5.append(fila)
ok &= comparar("Tabla 5 diferencias pareadas", filas(tb[1]), e5)
orden = ["antecedentes", "soporte", "infecciones", "falla", "complicaciones", "egreso"]
e6 = []
for bl in orden:
    base = ab[ab.Bloque == bl].iloc[0]
    fila = [base.n_campos, base.kappa_humano_prom]
    for m in MODELOS:
        for a in "ABC":
            fila.append(float(ab[(ab.Bloque == bl) & (ab.Modelo == m) & (ab.Estrategia == a)].exactitud_balanceada.iloc[0]))
    e6.append(fila)
ok &= comparar("Tabla 6 acuerdo por bloque", filas(tabulares(entorno("tab:acuerdo-bloque"))[0]), e6)
e7 = [[g("todos", m, a, "faltante_como_error", "exactitud_balanceada"), g("todos", m, a, "respondidas", "exactitud_balanceada"),
       g("todos", m, a, "solo_validos", "exactitud_balanceada"), g("todos", m, a, "faltante_como_error", "cobertura_pct")]
      for m in MODELOS for a in "ABC"]
ok &= comparar("Tabla 7 sensibilidad", filas(tabulares(entorno("tab:sensibilidad"))[0]), e7)
sys.exit(0 if ok else 1)
