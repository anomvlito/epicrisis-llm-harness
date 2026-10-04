# Extracción de variables clínicas desde epicrisis con modelos de lenguaje

Código, instrucciones (*prompts*) y agregados de la comparación de tres estrategias de extracción
(consulta única, *harness* por grupos y *harness* con modelo del caso) sobre 50 epicrisis, corrida
final `form199-v2.6` (protocolo 1.9). Acompaña el informe del Trabajo de Título de Fabián Ortega
(Escuela de Ingeniería, Pontificia Universidad Católica de Chile, 2026); los prompts se transcriben
en su Anexo E.

## Contenido

| Carpeta | Qué contiene |
|---|---|
| `harness/` | Código congelado de la corrida final: protocolo y validador (`protocol.py`), estrategias A, B y C (`monolithic.py`, `runner.py`, `arm_c.py`), motor vLLM (`engine_vllm.py`), resumen de corridas, 61 pruebas unitarias, configuración y script SLURM. |
| `harness/prompts/` | Prompt común (`common.md`), instrucciones del modelo del caso (`case_model.md`) y los 18 prompts de grupo de la estrategia B. |
| `harness/form_schema.json` | Esquema del formulario (199 variables y sus nodos contenedores). No contiene datos de pacientes. |
| `analysis/` | Agregados de la comparación contra la referencia humana (CSV y JSON, sin identificadores ni texto clínico) y los scripts que generan las tablas y figuras del informe. |
| `env/` | Entorno de la corrida (`vllm-form199.yml` y lista de paquetes congelados): vLLM 0.30.0, torch 2.13.0, transformers 5.17.0. |
| `exploratory/` | Código y prompts de la etapa exploratoria de julio de 2026 (Transformers con cuantización a 4 bits). Es histórico: no es el procedimiento de la corrida final. |

## Qué no incluye

- El manual de anotación del equipo clínico.
- Los textos de las epicrisis, las salidas por caso, las trazas y la lista de casos de la cohorte.
- Los pesos de los modelos (Gemma 4 31B, Qwen 3.6 35B-A3B y Llama 3.3 70B) y la plataforma de anotación.
- Los módulos de análisis de acuerdo entre anotadores que `analysis/analisis_estrategias.py` importa
  (`analizar_modelos_vs_humano`, `analizar_concordancia`), desarrollados por otro integrante del equipo.

## Qué se puede verificar y qué no

- **Sin datos ni clúster:** las pruebas unitarias (`cd harness && python3 -m unittest`, 61 pruebas, 1 omitida), la
  lectura de los prompts y del esquema, y las cifras agregadas de `analysis/`.
- **Con los datos y el clúster:** repetir la corrida exige las 50 epicrisis anonimizadas, el manual de anotación
  (`runner.py` comprueba que el del conjunto de datos coincida con el del código), los modelos y GPU. Las rutas de
  ese entorno aparecen como `/path/to/models` y `/path/to/workspace`.
- `analysis/tablas_anexo_c.py` y los scripts de figuras están escritos para la estructura de carpetas del
  repositorio de la tesis; aquí se entregan con sus agregados para trazabilidad.

## Uso de las pruebas

```bash
cd harness
python3 -m unittest test_parser test_protocol test_prompt_examples test_batch_driver \
  test_arm_c test_monolithic test_sensitivity test_inference_setup
```

Repositorio privado. Sin licencia: todos los derechos reservados.
