## Tarea de esta llamada: modelo del caso

No respondas todavía el formulario. Construye un MODELO DEL CASO: una representación
estructurada y verificable del episodio que después usarán, por separado, 18 llamadas
que responden grupos de variables del formulario. Su propósito es que todas partan de
la misma lectura del episodio: dónde empieza y termina la primera estadía en UCI, qué es
antecedente y qué es agudo, qué soportes se realizaron, cómo evolucionaron las
infecciones y cuál fue el desenlace.

Reglas:
- Solo afirmaciones respaldadas por la epicrisis. Cada elemento lleva `evidence_ids` con
  al menos un ID de línea existente. No inventes fechas, lugares ni diagnósticos.
- Si algo se deduce y no está escrito literalmente, indícalo en `certeza` o en `dudas`;
  no lo presentes como hecho.
- Distingue la hospitalización de las estadías en UCI/UPC. Si la epicrisis no documenta
  ninguna estadía en UCI, usa `hubo_estadia_uci: false`, deja `estadias_uci` vacío,
  `primera_estadia: null` y no llenes el desenlace de UCI.
- `momento` sitúa cada evento respecto de la PRIMERA estadía en UCI.
- `problemas`: condiciones clínicas con su estado (`antecedente_cronico` si existía antes
  del ingreso a UCI; `agudo_del_episodio` si ocurrió durante la hospitalización) y su
  certeza (afirmado, negado, sospecha descartada, sospecha no resuelta). Registra como
  `antecedente_cronico` solo lo que la nota presenta como antecedente o condición previa.
  No deduzcas enfermedades a partir de un fármaco, un examen, una complicación o una
  sigla: si solo hay un indicio así, va en `dudas`, no en `problemas`.
- Siglas y abreviaturas: expándelas solo si su significado es inequívoco en el contexto
  de la nota. Si una sigla admite más de un significado clínico (por ejemplo, una
  característica de un tratamiento o un diagnóstico), no la conviertas en problema ni en
  soporte: regístrala en `dudas` con su evidencia.
- `soportes`: registra los soportes realizados y marca `realizado: false` si solo fue
  planificado o indicado. Un soporte que la nota no menciona no se registra.
- `infecciones`: una entrada por infección diagnosticada. Fiebre, exámenes alterados o un
  cultivo aislado no bastan; la colonización no es infección.
- `calidad_documental`: secciones de la plantilla vacías, abreviaturas que expandiste,
  contradicciones y datos que el formulario suele pedir y la epicrisis no documenta.
- `dudas`: lo que no pudiste resolver con la lectura, con su evidencia.
- Sé breve: textos cortos, sin razonamiento paso a paso. Fechas en DD/MM/AAAA o null.

Devuelve SOLO un objeto JSON con exactamente estas claves y tipos:

```json
{
  "episodio": {
    "hubo_estadia_uci": true,
    "estadias_uci": [{"orden": 1, "ingreso": "DD/MM/AAAA", "egreso": "DD/MM/AAAA",
                      "origen": "texto breve o null", "destino": "texto breve o null",
                      "evidence_ids": ["E0001"]}],
    "primera_estadia": 1
  },
  "linea_de_tiempo": [{"momento": "antes_uci", "dia_relativo": null, "evento": "texto breve",
                       "tipo": "ingreso", "certeza": "afirmado", "evidence_ids": ["E0001"]}],
  "problemas": [{"problema": "texto breve", "estado": "antecedente_cronico",
                 "certeza": "afirmado", "evidence_ids": ["E0001"]}],
  "soportes": [{"soporte": "texto breve", "realizado": true, "inicio": null, "fin": null,
                "evidence_ids": ["E0001"]}],
  "infecciones": [{"diagnostico": "texto breve", "sepsis": false, "foco": null, "germen": null,
                   "tratamiento": null, "evidence_ids": ["E0001"]}],
  "desenlace_primera_uci": {"estado_vital": null, "destino": null, "evidence_ids": []},
  "calidad_documental": {"secciones_vacias": [], "abreviaturas": {}, "contradicciones": [],
                         "no_documentado": []},
  "dudas": [{"tema": "texto breve", "por_que": "texto breve", "evidence_ids": ["E0001"]}]
}
```

Valores permitidos:
- `momento`: antes_uci, durante_primera_uci, despues_primera_uci, sin_uci.
- `tipo`: ingreso, traslado, procedimiento, soporte, infeccion, complicacion, falla_organica, egreso, otro.
- `certeza`: afirmado, negado, sospecha_descartada, sospecha_no_resuelta.
- `estado`: antecedente_cronico, agudo_del_episodio.
- `sepsis`: true, false o "unknown".
- `estado_vital`: Vivo, Fallecido o null.
- `contradicciones`: lista de objetos {"descripcion": "texto breve", "evidence_ids": [...]}.
- Escribe cada valor permitido exactamente como aparece arriba, sin tildes ni variantes
  (`falla_organica`, no `fallo_organico`; `infeccion`, no `infección`). Si un evento no
  calza en ningún `tipo`, usa `otro`; un antecedente crónico va en `problemas`, no en la
  línea de tiempo.
- `abreviaturas` es un objeto sigla → significado en el que cada sigla aparece una sola
  vez, por ejemplo {"VMI": "ventilación mecánica invasiva", "DVA": "drogas vasoactivas"}.
  Nunca repitas una clave en un mismo objeto; si una sigla es ambigua, anótala en `dudas`.
- Todo elemento de una lista lleva al menos un ID de evidencia existente, escrito entre
  comillas y sin corchetes ("E0012").
