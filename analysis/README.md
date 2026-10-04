# Comparación de estrategias de extracción contra la referencia humana (form199-v2.6)

Fuente de las cifras de las secciones de resultados y discusión del informe sobre las tres
estrategias de extracción. Solo contiene agregados: sin IDs de pacientes, sin texto clínico
y sin respuestas por caso.

## Procedencia

- **Corrida:** form199-v2.6 (protocolo form199-v1.9), 50 epicrisis de la cohorte
  `concordancia-50-v1` (snapshot `concordancia-50-20260924`), tres estrategias (A consulta única,
  B *harness* de 18 llamadas, C *harness* con modelo del caso) y tres modelos (Gemma4, Qwen,
  Llama-70B). Motor vLLM 0.30.0 con pesos FP8, igual configuración en las tres estrategias de cada
  modelo. Resultados en Cóndor: `/path/to/workspace/experiments/concordancia50-form199-v2.6/`
  (`monolithic-b16384`, `harness`, `armc-r3`).
- **Referencia humana:** exportación del experimento de concordancia al 29-09-2026 12:19
  (104 entregas; 44 epicrisis con 2 o 3 anotadores, 12 con 3).
- **Métricas de acuerdo:** siguen el procedimiento de análisis desarrollado por el ingeniero del
  equipo (kappa de Fleiss y AC1 por campo; referencia por mayoría; F1, sensibilidad, especificidad
  y exactitud balanceada; kappa *leave-one-out*). En este trabajo se extendió a las tres
  estrategias (`analisis_estrategias.py`, que importa sus funciones sin modificarlas). La
  extensión reproduce exactamente sus cifras de la estrategia A con Gemma4 y Qwen.
- **"AUC" del análisis original = exactitud balanceada:** el modelo entrega etiquetas Sí/No, así que
  el área bajo la curva ROC coincide con (sensibilidad + especificidad) / 2.

## Alcance en el informe

- **Llama-70B queda fuera de la comparación del informe** por decisión del autor (30-09-2026): con la
  consulta única no produjo ninguna salida válida en los 50 casos y respondió el 61,9 % de los campos,
  de modo que no admite la comparación pareada entre estrategias. Sus agregados se conservan en estos
  archivos por trazabilidad; el cuerpo del informe solo describe su exclusión (sección 3.7, trabajo
  futuro y Anexo C.1) y no lo incluye en las tablas.
- Las **Tablas 2 a 7** del cuerpo se transcriben de estos archivos (se comprueban cifra por cifra con `comprobar_tablas_cuerpo.py`); las Figuras 5 y 6 se regeneran con `figura_kappa_bloque.py` y `figura_estrategias.py`.
- El **Anexo C** del informe (`../../anexos/anexo_c_{concordancia,acuerdo,sensibilidad}.tex`) se genera con `tablas_anexo_c.py`
  a partir de estos CSV; `etiquetas_formulario.json` traduce las claves de las variables a las
  etiquetas del formulario.

## Decisiones del análisis

- **Campos:** variables booleanas con prevalencia humana ≥ 5 % y kappa humano calculable (109, como
  en el análisis original), restringidas a las 90 que el formulario del modelo pide. Las otras 19
  son nodos agrupadores de la plataforma (p. ej. `antecedentes`) que no se piden al modelo y que en
  la convención original aportan n = 0.
- **Tramos de kappa humano:** ≥ 0,8 (29 campos), 0,6–0,8 (28) y < 0,6 (33).
- **Variantes:** `faltante_como_error` (principal: si hay referencia y el modelo no entregó un valor
  booleano, cuenta como error), `respondidas` (convención original: se omite el par) y
  `solo_validos` (solo casos que pasan el validador del protocolo).
- **Diferencias entre estrategias:** *bootstrap* pareado por paciente (44 pacientes con referencia,
  2000 remuestreos, semilla 20260930), IC 95 % por percentiles.

## Archivos

| Archivo | Contenido |
|---|---|
| `estructura_y_costo.json` | Validez, errores por categoría, campos respondidos, llamadas, tokens y tiempo de GPU por modelo y estrategia (generado con `summarize_runs.py` de form199-v2.6 en Cóndor) |
| `acuerdo_por_estrategia.csv` | Matriz de confusión y métricas micro por estrategia × modelo × tramo × variante, con cobertura |
| `diferencias_pareadas_bootstrap.csv` | B−A, C−B y C−A con IC 95 % |
| `kappa_loo_por_campo.csv`, `kappa_loo_por_tramo.csv` | Kappa al reemplazar a un anotador por el modelo |
| `acuerdo_por_bloque.csv` | Exactitud balanceada por bloque del formulario (variante principal) |
| `campos_analizados.csv`, `meta.json` | Los 90 campos, su tramo y parámetros del análisis |
| `concordancia_por_campo.csv`, `resumen_por_categoria.csv` | Concordancia humana por campo y por bloque (análisis del ingeniero del equipo, corte 29-09) |
| `concordancia_humana_resumen.json` | Resumen de la concordancia humana sobre los 171 campos activos |
| `acuerdo_por_campo_estrategia.csv` | Exactitud balanceada, F1 y cobertura por campo × estrategia × modelo (Anexo C) |
| `acuerdo_por_referencia.csv` | Sensibilidad a la referencia: mayoría frente a unanimidad (Anexo C) |
| `etiquetas_formulario.json` | Clave de variable → etiqueta del formulario (222 nodos) |
| `analisis_estrategias.py`, `figura_estrategias.py`, `figura_kappa_bloque.py`, `tablas_anexo_c.py` | Scripts de la extensión, de las figuras y de las tablas del Anexo C |
