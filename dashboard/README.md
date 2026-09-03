# dashboard

Visualización del sistema de alerta temprana (Streamlit). Servicio de solo
lectura: no calcula nada, solo lee lo que producen `modelo-temporal`,
`espacializacion` y el estado de `orquestador`.

## Build

```bash
cd dashboard
docker build -t dashboard:test -f Dockerfile .
```

## Correr

Servicio persistente (no forma parte de la corrida semanal del
orquestador): se deja corriendo aparte, ej.:

```bash
cd /home/tomas/sistema_alerta_temprana
docker run -d --name dashboard --restart unless-stopped -p 8501:8501 \
  -v $(pwd)/espacializacion/output:/app/../espacializacion/output:ro \
  -v $(pwd)/espacializacion/estaticas:/app/../espacializacion/estaticas:ro \
  -v $(pwd)/espacializacion/data/vegetacion:/app/../espacializacion/data/vegetacion:ro \
  -v $(pwd)/modelo-temporal/output:/app/../modelo-temporal/output:ro \
  -v $(pwd)/orquestador/logs:/app/../orquestador/logs:ro \
  dashboard:test
```

Abrir `http://localhost:8501`.

Los cinco volúmenes son de solo lectura: el dashboard nunca escribe nada,
solo lee los TIFFs de índice de actividad, las 3 variables estáticas del
MCDA y el NDVI semanal (para la sección "Variables espaciales"), los CSV
del modelo temporal, y
`orquestador/logs/estado_ultima_corrida.json` (para el cartel de error).

## Correr en desarrollo (sin Docker)

```bash
cd dashboard
pip install -r requirements.txt
streamlit run app.py
```

Funciona igual fuera de Docker porque `REPO_ROOT` se calcula relativo al
propio archivo (`dashboard/app.py` → sube dos niveles).

## Qué muestra

- Selector de localidad (4) y semana (default: la más reciente).
- Mapa Leaflet del índice de actividad, clasificación categórica (4 clases,
  umbral de Youden propio de cada localidad). Capa de error (σ) opcional,
  apagada por defecto.
- Curva del índice de oviposición: tramo confirmado vs. pronosticado (CFS,
  14 días) diferenciado.
- Serie meteorológica cruda, colapsada por defecto.
- Cartel de error arriba de todo, *solo si* la última corrida semanal del
  orquestador tuvo algún paso fallido (lee `estado_ultima_corrida.json`).
- Pestaña "Acerca de" con metodología y fuentes de datos.
