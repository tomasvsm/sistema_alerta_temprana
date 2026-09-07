#!/bin/bash
# Corre calculo_mcda.py para las 4 localidades sin pedir input interactivo.
# Paso 4 del orquestador semanal -- corre despues de que la vegetacion de
# la semana actual ya este generada (run_veg_backfill.sh).
#
# Corre en el contenedor geoprocesos:test (ya existia, se usaba solo para
# indice_actividad) en vez de python3 del host -- verificado 2026-09-07
# (87 semanas de una localidad, 0 diferencias contra la salida del host)
# antes de cambiarlo. Sin GRASS de por medio (calculo_mcda.py es rasterio/
# numpy puro), asi que el riesgo de deriva de resultado era bajo, pero se
# cambia igual para que ningun paso del pipeline semanal dependa de tener
# python3/rasterio instalados en el host que lo dispare.
set -uo pipefail
cd "$(dirname "$0")/.."
echo "1252,1271,1300,1385" | docker run --rm -i \
  -v "$(pwd)/data/vegetacion:/app/data/vegetacion:ro" \
  -v "$(pwd)/output/MCDA:/app/output/MCDA" \
  geoprocesos:test python3 src/calculo_mcda.py
