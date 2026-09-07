#!/bin/bash
# Corrida de variables estaticas (construcciones/poblacion/NBI) para MCDA.
#
# A diferencia de vegetacion, esto NO es periodico: las variables son
# estaticas (no cambian semana a semana), asi que cada localidad se procesa
# una sola vez y de ahi en mas queda salteada para siempre. El uso normal
# de este script es al agregar una localidad nueva: agregar una linea a
# GIDS de abajo y volver a correrlo -- las localidades ya procesadas se
# saltean solas (no vuelve a pisar nada).
#
# Prerequisito para una localidad nueva: que ya exista su ROI cacheado en
# resources/roi/roi_gid_<gid>_*.gpkg (lo genera calculo_vegetacion.py,
# opcion 2 del prompt de ROI: "Construir ROI desde shapefile de municipios,
# filtrar por gid + buffer"). Si no existe, este script la saltea con FALLO
# en vez de intentar adivinar un ROI.
set -uo pipefail

cd /home/tomas/sistema_alerta_temprana/espacializacion

GRASS_MAPSET="/home/tomas/grassdata/posgar2007_4_cba/MCDA"
LOGDIR="/home/tomas/sistema_alerta_temprana/espacializacion/scripts/estaticas_logs"
mkdir -p "$LOGDIR"

declare -A GIDS=(
  [1385]="cordoba"
  [1300]="rio_cuarto"
  [1252]="villa_maria"
  [1271]="salsipuedes"
)

# $1 (opcional): correr solo este gid (ej. al agregar una localidad nueva),
# en vez de recorrer las 4 ya conocidas.
if [ $# -ge 1 ]; then
  GIDS_A_CORRER=("$1")
else
  GIDS_A_CORRER=("${!GIDS[@]}")
fi

N=0
OK=0
FAIL=0
SKIP=0
TOTAL=${#GIDS_A_CORRER[@]}
echo "=== Variables estaticas: $TOTAL localidad(es) ==="

for GID in "${GIDS_A_CORRER[@]}"; do
  N=$((N+1))
  NOMBRE="${GIDS[$GID]:-desconocida}"
  RUN_NAME="gid_${GID}_estaticas"
  # Buildings_cat_100m es el ultimo archivo que escribe process_construcciones,
  # y construcciones corre antes que poblacion/NBI en main() -- si este
  # archivo existe, la corrida anterior termino los 3 pasos.
  RESULTADO_FINAL="estaticas/${RUN_NAME}/construcciones/${RUN_NAME}_Buildings_cat_100m.tif"

  if [ -f "$RESULTADO_FINAL" ]; then
    echo "[$N/$TOTAL] gid=$GID ($NOMBRE) ... SALTEADO (ya existe)"
    SKIP=$((SKIP+1))
    continue
  fi

  if ! compgen -G "resources/roi/roi_gid_${GID}_*.gpkg" > /dev/null; then
    echo "[$N/$TOTAL] gid=$GID ($NOMBRE) ... FALLO (no hay ROI cacheado en resources/roi/, generarlo primero con calculo_vegetacion.py)"
    FAIL=$((FAIL+1))
    continue
  fi

  LOGFILE="$LOGDIR/gid_${GID}.log"
  echo "[$N/$TOTAL] gid=$GID ($NOMBRE) ..."
  grass "$GRASS_MAPSET" --exec python3 src/variables_MCDA.py "$GID" > "$LOGFILE" 2>&1
  RC=$?
  if [ $RC -eq 0 ] && [ -f "$RESULTADO_FINAL" ]; then
    echo "    OK"
    OK=$((OK+1))
  else
    echo "    FALLO (rc=$RC) -> revisar $LOGFILE"
    FAIL=$((FAIL+1))
  fi
done

echo ""
echo "=== VARIABLES ESTATICAS TERMINADO: $OK ok, $FAIL fallidas, $SKIP salteadas de $TOTAL ==="
