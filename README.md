# sistema_alerta_temprana

Sistema de alerta temprana para *Aedes aegypti* en 4 localidades de
Córdoba: Córdoba capital (gid 1385), Río Cuarto (1300), Villa María (1252) y
Salsipuedes (1271).

Combina un **modelo temporal** (dinámica poblacional Aguirre/Otero a partir
de clima) con un **modelo espacial** (MCDA de idoneidad de hábitat a partir
de NDVI, población, NBI y construcciones) en un **índice de actividad**
semanal por píxel: `IdA = idoneidad_espacial × índice_de_oviposición`.

La arquitectura son contenedores Docker independientes que solo se
comunican a través de un volumen de datos compartido, sin llamadas
directas entre ellos:

| Servicio | Qué hace |
|---|---|
| `modelo-temporal` | descarga clima, corre el modelo, calcula índice de oviposición |
| `vegetacion` | descarga Sentinel-2, calcula NDVI semanal categorizado |
| `capas-estaticas` | procesa población, construcciones y NBI para el MCDA |
| `geoprocesos` | MCDA (idoneidad espacial) e índice de actividad final |
| `orquestador` | script del host que coordina la corrida semanal (no es un contenedor) |
| `dashboard` | visualización (Streamlit), servicio persistente aparte |

Las credenciales (IMERG, GDAS/GDEX, Copernicus/EODAG) van en
`*/resources/passwords.cfg`, que está gitignoreado: hay que pedir las
claves aparte, no están en el repo.

---

## modelo-temporal

### Build

```bash
cd modelo-temporal
docker build -t modelo-temporal:test -f Dockerfile .
```

### 1. Descargar clima: histórico / backfill grande

Para poblar desde cero o extender bien hacia atrás (por ejemplo al agregar
una localidad nueva, o ampliar el spin-up del modelo). Usa GDEX (GDAS/FNL)
y GES DISC (IMERG) en paralelo:

```bash
cd modelo-temporal
# GDAS vía GDEX (pedido asíncrono del lado del servidor)
python3 -c "
import sys; sys.path.insert(0,'src')
import gdas_lib, datetime
rid = gdas_lib.submit(datetime.date(2023,1,1), datetime.date(2024,6,30))
print(rid)
"
# esperar a que gdas_lib.get_status(rid) devuelva 'Completed', después:
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

Después, extraer el CSV por localidad (lat/lon están en
`resources/get_weather.cfg`):

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

`daterange()` excluye el último día del rango: si tiene que incluirlo, hay
que extender `end_date` en 1 día.

### 2. Descargar clima: operativo (ventana chica, por ejemplo la última semana)

Para actualizaciones cortas alcanza con el flujo simple (NOMADS + IMERG +
pronóstico en un solo llamado):

```bash
cd modelo-temporal
python3 src/get_weather.py 2026-08-18 2026-08-25
```

`get_weather.py` siempre necesita las 2 fechas como argumento; correrlo
sin argumentos no funciona.

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

`start_date` se fija en 2023-01-01 aunque el período que interesa empiece
después: el modelo necesita alrededor de 1.5 años de rodaje antes de que
la ventana de normalización del índice de oviposición (365 días móviles)
sea confiable. Arrancar muy cerca de la fecha de interés contamina el
mínimo de esa ventana con el transitorio inicial, ya que el modelo parte
de una población arbitraria sin adultos.

Salida por localidad: `output/{rango}_{gid}_{nombre}_modelo.csv` (crudo:
huevos, larvas, pupas, adultos, tasa de oviposición y clima) y
`output/{rango}_{gid}_{nombre}_indice_oviposicion.csv` (estandarizado
entre 0 y 1).

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

Corre GRASS GIS dentro de un contenedor con base `ubuntu:24.04`: el repo
`universe` de Ubuntu 24.04 distribuye `grass-core` en la misma versión
exacta que corre en el host (8.3.2), lo que evita diferencias de resultado
por versión.

Build:

```bash
cd espacializacion
docker build -t vegetacion:test -f vegetacion.Dockerfile .
```

Corrida de una semana, interactiva (pide localidad, fecha y ROI):

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

Montar `/home/tomas/grassdata` (el mapset real) evita reprocesar todo
desde cero en cada corrida. Sin ese volumen, el contenedor arranca con la
location vacía que trae la imagen, que solo sirve para un despliegue nuevo
en una máquina sin mapset previo.

Backfill de un rango de semanas, con `scripts/run_veg_backfill.sh`: recorre
4 localidades por N semanas (desde una fecha fija hasta hoy), salteando lo
que ya existe en `data/vegetacion/`. Es seguro relanzarlo después de una
interrupción, retoma solo lo que falta:

```bash
cd espacializacion
nohup bash scripts/run_veg_backfill.sh >> scripts/veg_backfill_master.log 2>&1 &
echo $! > scripts/veg_backfill_pid.txt
```

Para pausarlo de forma segura hay que esperar a ver `OK` en
`veg_backfill_master.log` para la semana en curso antes de matar el
proceso: si se lo mata a mitad de una semana, deja una carpeta parcial que
después se saltea como si estuviera completa. Si eso pasa, hay que borrar
esa carpeta puntual en `data/vegetacion/`.

### capas-estaticas: población, NBI y construcciones para el MCDA

Corre en Docker igual que vegetación, con la misma base y la misma versión
de GRASS. A diferencia de vegetación, no es periódico: los datasets son
estáticos, se procesan una vez por localidad y quedan.

Build:

```bash
cd espacializacion
docker build -t capas-estaticas:test -f capas_estaticas.Dockerfile .
```

`scripts/run_variables_estaticas.sh` corre las 4 localidades conocidas
salteando las que ya están hechas, así que relanzarlo no repite trabajo:

```bash
cd espacializacion
bash scripts/run_variables_estaticas.sh          # las 4 conocidas, saltea lo hecho
bash scripts/run_variables_estaticas.sh 1300     # solo un gid puntual
```

También se puede correr manualmente, interactivo, pidiendo el gid por
input:

```bash
cd espacializacion
grass /home/tomas/grassdata/posgar2007_4_cba/MCDA --exec python3 src/variables_MCDA.py
```

Depende de datasets externos grandes (FABDEM, WorldPop, NBI, Open
Buildings) que no están en el repo: las rutas están en las constantes
`PATH_*` al principio de `src/variables_MCDA.py`.

#### Agregar una localidad nueva

Integrar una localidad nueva implica tocar 8 lugares del sistema, en este
orden:

1. **ROI**: generarlo con `calculo_vegetacion.py` (opción 2 del prompt:
   "Construir ROI desde shapefile de municipios, filtrar por gid y
   buffer"), queda cacheado en `resources/roi/roi_gid_<gid>_*.gpkg`. Sin
   esto, `run_variables_estaticas.sh` la saltea con error.
2. **Variables estáticas**: `bash scripts/run_variables_estaticas.sh <gid>`.
   Conviene confirmar antes que los datasets externos (FABDEM, Open
   Buildings, WorldPop, NBI) cubren la localidad nueva.
3. **Clima**: agregar `[nombre]` con `lat`/`lon` en
   `modelo-temporal/resources/get_weather.cfg`, y correr un backfill
   histórico (sección "Descargar clima: histórico" más arriba).
4. **Modelo temporal**: agregar `(gid, nombre)` a la lista de localidades
   en `modelo-temporal/src/correr_modelo_4loc.py`.
5. **Vegetación**: agregar `[nombre]="resources/roi/roi_gid_<gid>_*.gpkg"`
   al `ROIS` de `espacializacion/scripts/run_veg_backfill.sh`, y correr un
   backfill histórico.
6. **MCDA**: agregar `"<gid>": "nombre"` a `LOCALIDADES` en
   `espacializacion/src/calculo_mcda.py`, el gid a la lista de
   `espacializacion/scripts/correr_mcda_todas.sh`, y rebuildear
   `geoprocesos:test` — las variables estáticas del paso 2 quedan
   horneadas en esa imagen al buildear, no se leen en vivo.
7. **Índice de actividad**: agregar `"<gid>": "nombre"` a `LOCALIDADES` en
   `espacializacion/src/calculo_indice_actividad.py`.
8. **Dashboard**: agregar el gid a los diccionarios de nombres en
   `dashboard/app.py`, y calcular su umbral de Youden y sus bounds de
   oviposición. Estos dos valores requieren haber corrido el modelo con
   datos históricos suficientes para calibrarlos: no son arbitrarios. Ver
   `dashboard/README.md`.

Los pasos 3 a 8 tocan calibración (Youden, spin-up del modelo) que
requiere criterio, no solo ejecución, así que no hay un único script que
haga todo esto de punta a punta. Esta lista es la referencia para no
saltear ningún paso.

### MCDA (idoneidad espacial)

No usa GRASS: es rasterio y numpy puro. Corre en el mismo contenedor
`geoprocesos:test` que el índice de actividad.

```bash
cd espacializacion
echo "1252,1271,1300,1385" | docker run --rm -i \
  -v $(pwd)/data/vegetacion:/app/data/vegetacion:ro \
  -v $(pwd)/output/MCDA:/app/output/MCDA \
  geoprocesos:test python3 src/calculo_mcda.py
```

`estaticas/` (construcciones, población, NBI) se hornea en la imagen al
buildear. Si se agrega una localidad nueva hay que rebuildear
`geoprocesos:test` para que la vea.

### Índice de actividad (idoneidad × oviposición)

Build:

```bash
cd espacializacion
docker build -t geoprocesos:test -f geoprocesos.Dockerfile .
```

Corrida sin argumentos: escanea todos los MCDA disponibles y todas las
localidades, salteando lo ya calculado.

```bash
docker run --rm \
  -v $(pwd)/data/vegetacion:/app/data/vegetacion:ro \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/../modelo-temporal/output:/app/../modelo-temporal/output:ro \
  geoprocesos:test python3 src/calculo_indice_actividad.py
```

Salida: `output/indice_actividad/{fecha}_{gid}_indice_actividad.tif` y
`_sigma.tif` (desvío intra-semanal) por localidad y semana.

---

## orquestador

Un script del host (no un contenedor) que corre automáticamente los
miércoles, encadenando clima → modelo temporal → vegetación → MCDA →
índice de actividad. Ver `orquestador/README.md` para el detalle completo:
cómo correrlo a mano, cómo se calcula la fecha de referencia semanal, y el
formato del estado consolidado.

## dashboard

Visualización con Streamlit y Leaflet. Ver `dashboard/README.md` para
build y ejecución. Es un servicio persistente aparte, no forma parte de la
corrida semanal.

```bash
cd dashboard
docker build -t dashboard:test -f Dockerfile .
```

---

## Pendiente

Todos los pasos del pipeline semanal corren en contenedor: ninguno
depende de tener GRASS o Python instalados en la máquina que los dispara.
Lo que falta es la reproducibilidad de los *datos*, no del software:
FABDEM, Open Buildings, WorldPop y NBI (unos 20GB, bajo
`/home/tomas/gisdata/GIS_MCDA/`) son un volumen montado desde el host, sin
su procedencia documentada. Levantar el sistema en un servidor nuevo
requiere copiar ese directorio a mano y documentar de dónde salió cada
dataset.
