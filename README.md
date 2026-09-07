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
| `vegetacion` | ✅ Dockerfile listo (GRASS 8.3.2 = host) | descarga Sentinel-2, calcula NDVI semanal categorizado |
| `geoprocesos` | ✅ Dockerfile listo | MCDA (idoneidad) + índice de actividad final |
| `capas-estaticas` | ✅ Dockerfile listo (GRASS 8.3.2 = host) | población/NBI/construcciones (datasets externos grandes, no versionados) |
| `orquestador` | ✅ Instalado y corriendo (cron martes) | corrida semanal automática |
| `dashboard` | ✅ Dockerfile listo | visualización (Streamlit) |

Credenciales (IMERG, GDAS/GDEX, Copernicus/EODAG) van en
`*/resources/passwords.cfg`, gitignoreado: pedir las claves aparte, no están
en el repo.

---

## modelo-temporal

### Build

```bash
cd modelo-temporal
docker build -t modelo-temporal:test -f Dockerfile .
```

### 1. Descargar clima: histórico / backfill grande

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

⚠️ `daterange()` es exclusivo del último día: si el rango debe incluir el
día final, extender el `end_date` en 1 día.

### 2. Descargar clima: operativo (ventana chica, ej. última semana)

Para actualizaciones cortas alcanza con el flujo simple (NOMADS + IMERG +
forecast en un solo llamado):

```bash
cd modelo-temporal
python3 src/get_weather.py 2026-08-18 2026-08-25
```

⚠️ Correr `get_weather.py` **sin argumentos** está roto (fecha hardcodeada
2015-2024 dentro del `elif len(sys.argv)==1`): siempre pasar las 2 fechas.

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
importar: si se corre desde muy cerca de la fecha que interesa, el mínimo
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

### vegetacion: NDVI semanal por Sentinel-2

GRASS dockerizado (2026-09-07): la imagen usa `ubuntu:24.04` como base, no
`debian:bookworm-slim`, a propósito -- el repo `universe` de Ubuntu 24.04
(noble) tiene `grass-core` en la versión **exacta** que corre en el host
(8.3.2-1ubuntu2); Debian bookworm sólo ofrece GRASS 8.2.1, una versión
distinta que podría dar resultados ligeramente distintos en resampleos y
cálculos. Verificado además con una corrida real (semana ya procesada,
Sentinel-2 real): rasters de salida bit-idénticos a los del host.

Build:

```bash
cd espacializacion
docker build -t vegetacion:test -f vegetacion.Dockerfile .
```

Corrida de **una semana**, interactiva (pide localidad, fecha, ROI):

```bash
docker run --rm -it \
  -v /home/tomas/grassdata:/grassdata \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/resources/roi:/app/resources/roi \
  -v $HOME/.config/eodag/eodag.yml:/root/.config/eodag/eodag.yml:ro \
  vegetacion:test bash
# adentro:
grass /grassdata/posgar2007_4_cba/MCDA --exec python3 src/calculo_vegetacion.py
```

Montar `/home/tomas/grassdata` (el mapset real) es lo que hace que esto no
vuelva a importar/reprocesar desde cero cada vez -- sin ese volumen, el
contenedor arrancaría con la location vacía que trae la imagen (útil sólo
para un deploy nuevo en otra máquina, donde no existe mapset previo).

**Backfill de un rango de semanas**: `scripts/run_veg_backfill.sh` (ahora
corre en el contenedor de arriba, no en GRASS del host). Recorre 4
localidades × N semanas (desde una fecha fija hasta hoy), salteando
automáticamente lo que ya existe en `data/vegetacion/`: así que es seguro
relanzarlo después de una interrupción, retoma solo lo que falta:

```bash
cd espacializacion
nohup bash scripts/run_veg_backfill.sh >> scripts/veg_backfill_master.log 2>&1 &
echo $! > scripts/veg_backfill_pid.txt
```

Para pausarlo de forma segura (nunca matarlo a mitad de una semana: deja
una carpeta parcial que después se saltea como si estuviera completa):
esperar a ver `OK` en `veg_backfill_master.log` para la semana en curso,
recién ahí matar los procesos y, si igual quedó algo a mitad de camino,
borrar esa carpeta específica en `data/vegetacion/`.

### capas-estaticas: población/NBI/construcciones para MCDA

También dockerizado 2026-09-07 (misma imagen base y misma razón que
vegetación: GRASS 8.3.2 exacto). Verificado reprocesando una localidad ya
calculada (gid 1300): los 3 rasters categorizados a 100m dieron
bit-idénticos a los del host (0 píxeles distintos).

Build:

```bash
cd espacializacion
docker build -t capas-estaticas:test -f capas_estaticas.Dockerfile .
```

No es periódico como vegetación: son datasets estáticos, se procesan una
vez por localidad y quedan. `scripts/run_variables_estaticas.sh` corre las
4 localidades conocidas salteando las que ya están hechas (así que
relanzarlo no repite trabajo):

```bash
cd espacializacion
bash scripts/run_variables_estaticas.sh          # las 4 conocidas, saltea lo hecho
bash scripts/run_variables_estaticas.sh 1300     # solo un gid puntual
```

También se puede correr manualmente (interactivo, pide el gid por input):

```bash
cd espacializacion
grass /home/tomas/grassdata/posgar2007_4_cba/MCDA --exec python3 src/variables_MCDA.py
```

Depende de datasets externos grandes (FABDEM, WorldPop, NBI, Open
Buildings) que **no están en el repo**: rutas absolutas configuradas en
`PATH_*` al principio de `src/variables_MCDA.py`.

#### Agregar una localidad nueva (checklist completo)

`variables_MCDA.py` es un paso más de un total de 7 lugares que hay que
tocar para que una localidad nueva quede realmente integrada al sistema.
Orden sugerido:

1. **ROI**: generarlo con `calculo_vegetacion.py` (opción 2 del prompt:
   "Construir ROI desde shapefile de municipios, filtrar por gid + buffer")
   → queda cacheado en `resources/roi/roi_gid_<gid>_*.gpkg`. Sin esto,
   `run_variables_estaticas.sh` la saltea con FALLO.
2. **Variables estáticas**: `bash scripts/run_variables_estaticas.sh <gid>`
   (este paso). Confirmar que los datasets externos (FABDEM, Open
   Buildings, WorldPop, NBI) cubren la nueva localidad.
3. **Clima**: agregar `[nombre]` con `lat`/`lon` en
   `modelo-temporal/resources/get_weather.cfg`, y correr un backfill
   histórico (ver sección "Descargar clima: histórico" más arriba).
4. **Modelo temporal**: agregar `(gid, nombre)` a la lista de localidades
   en `modelo-temporal/src/correr_modelo_4loc.py`.
5. **Vegetación**: agregar `[nombre]="resources/roi/roi_gid_<gid>_*.gpkg"`
   al `ROIS` de `espacializacion/scripts/run_veg_backfill.sh`, y correr un
   backfill histórico (`nohup bash scripts/run_veg_backfill.sh &`, ver
   arriba).
6. **MCDA**: agregar `"<gid>": "nombre"` a `LOCALIDADES` en
   `espacializacion/src/calculo_mcda.py`, y el gid a la lista de
   `espacializacion/scripts/correr_mcda_todas.sh`.
7. **Índice de actividad**: agregar `"<gid>": "nombre"` a `LOCALIDADES` en
   `espacializacion/src/calculo_indice_actividad.py`.
8. **Dashboard**: agregar el gid a los diccionarios `NOMBRES`/slugs en
   `dashboard/app.py`, y calcular/agregar su umbral de Youden (`YOUDEN`) y
   sus bounds de oviposición -- estos dos requieren haber corrido el
   modelo con datos históricos suficientes para calibrarlos, no son un
   valor arbitrario. Ver `dashboard/README.md`.

No hay un solo script que haga todo esto de punta a punta: los pasos 3-8
tocan calibración (Youden, spin-up del modelo) que necesita juicio, no solo
ejecución. Este checklist es la referencia para no perder ningún paso.

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

Corrida (sin argumentos: escanea todos los MCDA disponibles y todas las
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

## orquestador

Automatización semanal (cron, martes) que encadena clima → modelo temporal
→ vegetación → MCDA → índice de actividad. Ver `orquestador/README.md` para
el detalle completo (cómo correrlo a mano, cómo está anclado el corte
semanal al martes, formato del estado consolidado).

## dashboard

Visualización (Streamlit + Leaflet). Ver `dashboard/README.md` para build y
run. Servicio persistente aparte, no forma parte de la corrida semanal.

```bash
cd dashboard
docker build -t dashboard:test -f Dockerfile .
```

---

## Pendiente

- **Software ya resuelto** (2026-09-07): vegetación y capas-estáticas
  corren en Docker con GRASS 8.3.2 (misma versión exacta que el host, vía
  `ubuntu:24.04` + `grass-core` del repo `universe` -- no la imagen
  `osgeo/grass-gis` planeada originalmente, que no garantizaba esa versión
  exacta). Todos los pasos del pipeline semanal corren en contenedor
  ahora, ninguno depende de tener GRASS/Python instalado en la máquina.
- **Datasets externos**: esto resuelve la reproducibilidad de *software*,
  no la de *datos*. FABDEM, Open Buildings, WorldPop y NBI (~20GB, bajo
  `/home/tomas/gisdata/GIS_MCDA/`) siguen siendo un volumen montado desde
  el host, con proveniencia sin documentar -- para levantar todo esto en
  un servidor nuevo hace falta copiar ese directorio y documentar de dónde
  salió cada dataset.
