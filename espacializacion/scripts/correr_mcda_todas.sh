#!/bin/bash
# Corre calculo_mcda.py para las 4 localidades sin pedir input interactivo.
# Paso 4 del orquestador semanal -- corre despues de que la vegetacion de
# la semana actual ya este generada (run_veg_backfill.sh).
set -uo pipefail
cd "$(dirname "$0")/.."
echo "1252,1271,1300,1385" | python3 src/calculo_mcda.py
