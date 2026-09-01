# sistema_alerta_temprana

Sistema de alerta temprana para *Aedes aegypti* (dengue) en 4 localidades de
Córdoba: Córdoba capital (gid 1385), Río Cuarto (1300), Villa María (1252) y
Salsipuedes (1271).

Combina un **modelo temporal** (dinámica poblacional Aguirre/Otero a partir
de clima) con un **modelo espacial** (MCDA de idoneidad de hábitat a partir
de NDVI, población, NBI y construcciones) en un **índice de actividad**
semanal por píxel: `IdA = idoneidad_espacial × índice_de_oviposición`.

Arquitectura: contenedores independientes que solo se comunican por un
volumen compartido (sin llamadas directas entre contenedores).

| Servicio | Estado | Qué hace |
|---|---|---|
| `modelo-temporal` | ✅ Dockerfile listo | descarga clima, corre el modelo, calcula índice de oviposición |
| `vegetacion` | ✅ Dockerfile listo | descarga Sentinel-2, calcula NDVI semanal categorizado |
| `geoprocesos` | ✅ Dockerfile listo | MCDA (idoneidad) + índice de actividad final |
| `capas-estaticas` | ⏳ pendiente | población/NBI/construcciones (datasets externos grandes, no versionados) |
| `orquestador` | ⏳ pendiente | corrida semanal automática (cron) |
| `dashboard` | ⏳ pendiente | visualización |

Credenciales (IMERG, GDAS/GDEX, Copernicus/EODAG) van en
`*/resources/passwords.cfg`, gitignoreado — pedir las claves aparte, no están
en el repo.

---

## modelo-temporal

### Build

```bash
cd modelo-temporal
docker build -t modelo-temporal:test -f Dockerfile .
```

### 1. Descargar clima — histórico / backfill grande

Para poblar desde cero o extender bien hacia atrás (ej. agregar una
localidad nueva, o ampliar el spin-up del modelo). Usa GDEX (GDAS/FNL) +
GES DISC (IMERG), en paralelo:

```bash
cd modelo-temporal
# GDAS vía GDEX (pedido asíncrono del lado del servidor)
python3 -c "
import sys; sys.path.insert(0,'src')
import gdas_lib, datetime
rid = gdas_lib.submit(datetime.date(2023,1,1), datetime.date(2024,6,30))
print(rid)
"
# ... esperar status == 'Completed' (gdas_lib.get_status(rid)), después:
python3 -c "
import sys; sys.path.insert(0,'src')
import gdas_lib
gdas_lib.download('<request_id>')
"

# IMERG (un archivo diario por fecha, se puede paralelizar)
python3 -c "
import sys, datetime; sys.path.insert(0,'src')
import get_weather as gw
gw.downloadDataFromIMERG(datetime.date(2023,1,1), datetime.date(2024,6,30), gw.IMERG_FOLDER)
"
```

Después, extraer el CSV por localidad (lat/lon en `resources/get_weather.cfg`):

```bash
python3 -c "
import sys, datetime; sys.path.insert(0,'src')
import get_weather as gw
from configparser import ConfigParser
cfg = ConfigParser(); cfg.read('resources/get_weather.cfg')
for loc in ['villa_maria','salsipuedes','cordoba','rio_cuarto']:
    lat, lon = float(cfg.get(loc,'lat')), float(cfg.get(loc,'lon'))
    gw.extractHistoricData(lat, lon, datetime.date(2023,1,1), datetime.date(2024,6,30), f'data/public/{loc}.csv')
"
```

⚠️ `daterange()` es exclusivo del último día — si el rango debe incluir el
día final, extender el `end_date` en 1 día.

### 2. Descargar clima — operativo (ventana chica, ej. última semana)

Para actualizaciones cortas alcanza con el flujo simple (NOMADS + IMERG +
forecast en un solo llamado):

```bash
cd modelo-temporal
python3 src/get_weather.py 2026-08-18 2026-08-25
```

⚠️ Correr `get_weather.py` **sin argumentos** está roto (fecha hardcodeada
2015-2024 dentro del `elif len(sys.argv)==1`) — siempre pasar las 2 fechas.

### 3. Correr el modelo (las 4 localidades)

```bash
cd modelo-temporal
python3 - <<'EOF'
import sys; sys.path.insert(0,'src')
from config import Configuration
import run as run_mod
import calcular_indice_oviposicion as cio

END_DATE = '2026-08-25'  # última fecha con clima disponible
localidades = [('1252','villa_maria'),('1271','salsipuedes'),
               ('1300','rio_cuarto'),('1385','cordoba')]

for gid, nombre in localidades:
    configuration = Configuration('resources/1c.cfg')
    configuration.config_parser.set('location','name',nombre)
    configuration.config_parser.set('simulation','start_date','2023-01-01')
    configuration.config_parser.set('simulation','end_date', END_DATE)
    configuration.config_parser.set('breeding_site','height','10')
    configuration.config_parser.set('breeding_site','amount','1')
    cfg_path = f'/tmp/run_{nombre}.cfg'
    configuration.save(cfg_path)
    modelo_csv = f'output/2023_2026_{gid}_{nombre}_modelo.csv'
    run_mod.main(cfg_path, modelo_csv, engine='cpp')
    cio.main(modelo_csv, f'output/2023_2026_{gid}_{nombre}_indice_oviposicion.csv')
EOF
```

`start_date` en 2023-01-01 (no el arranque real del período de interés) es
adrede: le da al modelo ~1.5 años de spin-up antes de que la ventana de
normalización del índice de oviposición (365 días móviles) empiece a
importar — si se corre desde muy cerca de la fecha que interesa, el mínimo
de esa ventana puede quedar contaminado por el transitorio de arranque
(población inicial arbitraria, sin adultos).

Salida por localidad: `output/{rango}_{gid}_{nombre}_modelo.csv` (crudo:
huevos/larvas/pupas/adultos/tasa_oviposicion/clima) y
`output/{rango}_{gid}_{nombre}_indice_oviposicion.csv` (estandarizado 0-1).

### Docker run

```bash
cd modelo-temporal
docker run --rm -v $(pwd)/data:/app/data -v $(pwd)/output:/app/output \
  -v $(pwd)/resources/passwords.cfg:/app/resources/passwords.cfg:ro \
  modelo-temporal:test bash
```

---

## espacializacion

### vegetacion — NDVI semanal por Sentinel-2

Build:

```bash
cd espacializacion
docker build -t vegetacion:test -f vegetacion.Dockerfile .
```

Corrida de **una semana**, interactiva (pide localidad, fecha, ROI):

```bash
docker run --rm -it -v $(pwd)/data:/app/data -v $(pwd)/resources:/app/resources \
  vegetacion:test bash
# adentro:
grass /grassdata/posgar2007_4_cba/MCDA --exec python3 src/calculo_vegetacion.py
```

**Backfill de un rango de semanas** (en host, no en Docker — necesita GRASS
instalado localmente): `scripts/run_veg_backfill.sh`. Recorre 4 localidades
× N semanas (desde una fecha fija hasta hoy), salteando automáticamente lo
que ya existe en `data/vegetacion/` — así que es seguro relanzarlo después
de una interrupción, retoma solo lo que falta:

```bash
cd espacializacion
nohup bash scripts/run_veg_backfill.sh >> scripts/veg_backfill_master.log 2>&1 &
echo $! > scripts/veg_backfill_pid.txt
```

Para pausarlo de forma segura (nunca matarlo a mitad de una semana — deja
una carpeta parcial que después se saltea como si estuviera completa):
esperar a ver `OK` en `veg_backfill_master.log` para la semana en curso,
recién ahí matar los procesos y, si igual quedó algo a mitad de camino,
borrar esa carpeta específica en `data/vegetacion/`.

### capas-estaticas — NDVI/población/NBI/construcciones para MCDA

```bash
cd espacializacion
python3 src/variables_MCDA.py   # pide el GID por localidad, interactivo
```

Depende de datasets externos grandes (FABDEM, WorldPop, NBI, Open
Buildings) que **no están en el repo** — rutas absolutas configuradas en el
propio script, se corre a demanda cuando se agrega/actualiza una localidad.

### MCDA (idoneidad espacial)

```bash
cd espacializacion
python3 src/calculo_mcda.py   # pide los GIDs separados por coma
```

### Índice de actividad (idoneidad × oviposición)

Build:

```bash
cd espacializacion
docker build -t geoprocesos:test -f geoprocesos.Dockerfile .
```

Corrida (sin argumentos — escanea todos los MCDA disponibles y todas las
localidades, saltea lo ya calculado):

```bash
docker run --rm \
  -v $(pwd)/data/vegetacion:/app/data/vegetacion:ro \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/../modelo-temporal/output:/app/../modelo-temporal/output:ro \
  geoprocesos:test python3 src/calculo_indice_actividad.py
```

Salida: `output/indice_actividad/{fecha}_{gid}_indice_actividad.tif` +
`_sigma.tif` (desvío intra-semanal) por localidad y semana.

---

## Pendiente

- `orquestador`: cron semanal — separar en dos entradas explícitas, una de
  backfill (rango de fechas, para poblar desde cero o agregar localidades)
  y otra operativa (sin argumentos, corre "esta semana" contra todos los
  servicios).
- `dashboard`.
- `capas-estaticas` containerizado (hoy corre en host).
