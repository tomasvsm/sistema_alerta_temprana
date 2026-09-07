#!/bin/bash
set -uo pipefail

cd /home/tomas/sistema_alerta_temprana/espacializacion

# Corre en el contenedor vegetacion:test (GRASS 8.3.2, misma version exacta
# que el host -- ver comentarios en vegetacion.Dockerfile), no en el GRASS
# del host: asi el paso no depende de que python3 resuelva a Anaconda (bug
# real encontrado 2026-09-07: bajo cron, sin Anaconda en el PATH, este
# paso fallaba por falta de geopandas/eodag) ni de tener GRASS instalado
# en la maquina que corre esto. El mapset se monta desde el host
# (/home/tomas/grassdata) para no perder el historial ya acumulado.
GRASS_MAPSET="/grassdata/posgar2007_4_cba/MCDA"
LOGDIR="/home/tomas/sistema_alerta_temprana/espacializacion/scripts/veg_backfill_logs"
mkdir -p "$LOGDIR"

declare -A ROIS=(
  [cordoba]="resources/roi/roi_gid_1385_1000m.gpkg"
  [rio_cuarto]="resources/roi/roi_gid_1300_1000m.gpkg"
  [villa_maria]="resources/roi/roi_gid_1252_1000m.gpkg"
  [salsipuedes]="resources/roi/roi_gid_1271_1000m.gpkg"
)

# $1 (opcional): fecha de referencia de la corrida (el martes ancla que
# pasa run_semanal.sh) -- sin esto el backfill usaba siempre la fecha
# real del sistema como limite superior, así que una corrida tardía
# (ej. jueves porque el martes no se pudo) podía llegar a generar una
# semana de vegetación de más respecto al resto del pipeline, que sí
# queda anclado al martes.
FECHA_REF="${1:-$(date +%Y-%m-%d)}"

FECHAS=()
cur="2025-01-07"
end="$FECHA_REF"
while [ "$(date -d "$cur" +%s)" -le "$(date -d "$end" +%s)" ]; do
  FECHAS+=("$cur")
  cur="$(date -d "$cur + 7 days" +%Y-%m-%d)"
done

TOTAL_RUNS=$(( ${#FECHAS[@]} * ${#ROIS[@]} ))
N=0
OK=0
FAIL=0
SKIP=0
echo "=== Backfill vegetacion: ${#ROIS[@]} localidades x ${#FECHAS[@]} semanas = $TOTAL_RUNS corridas ==="

for LOCALIDAD in "${!ROIS[@]}"; do
  ROI_PATH="${ROIS[$LOCALIDAD]}"
  for FECHA in "${FECHAS[@]}"; do
    N=$((N+1))
    FECHA_FIN="$(date -d "$FECHA + 7 days" +%Y-%m-%d)"
    OUTDIR="data/vegetacion/${LOCALIDAD}_${FECHA}_${FECHA_FIN}_vegetacion"
    OUTNAME="${LOCALIDAD}_${FECHA}_${FECHA_FIN}_vegetacion"
    RESULTADO_FINAL="$OUTDIR/outputs/final/${OUTNAME}_NDVI_cat_100m.tif"

    # Chequear el ARCHIVO FINAL, no la carpeta: calculo_vegetacion.py crea
    # el arbol de carpetas (ensure_dirs) antes de descargar/procesar nada,
    # asi que si el proceso se cae a mitad de camino (red, GRASS colgado,
    # OOM-kill) la carpeta queda creada pero incompleta -- con el chequeo
    # viejo (solo "existe la carpeta") esa semana quedaba salteada para
    # siempre en todos los reintentos futuros, sin volver a procesarse.
    if [ -f "$RESULTADO_FINAL" ]; then
      echo "[$N/$TOTAL_RUNS] $LOCALIDAD $FECHA ... SALTEADO (ya existe)"
      SKIP=$((SKIP+1))
      continue
    fi

    LOGFILE="$LOGDIR/${LOCALIDAD}_${FECHA}.log"
    echo "[$N/$TOTAL_RUNS] $LOCALIDAD $FECHA ..."
    # calculo_vegetacion.py trata la fecha ingresada como el EXTREMO
    # SUPERIOR de la ventana (resta DAYS_BACK=7 para el inicio) -- por eso
    # se le pasa FECHA_FIN, no FECHA, para que la ventana real resultante
    # sea [FECHA, FECHA_FIN] y coincida con OUTDIR. Antes se pasaba FECHA
    # por error, lo que generaba carpetas "(FECHA-7)_FECHA" en vez de
    # "FECHA_FECHA_FIN" -- el chequeo de "ya existe" nunca coincidia con lo
    # real y cada corrida reprocesaba la ultima semana en rango de nuevo
    # (sin corromper nada, pero desperdiciando descarga/computo).
    printf '%s\n%s\n1\n%s\n' "$LOCALIDAD" "$FECHA_FIN" "$ROI_PATH" \
      | docker run --rm -i \
          -v /home/tomas/grassdata:/grassdata \
          -v "$(pwd)/data:/app/data" \
          -v "$(pwd)/resources/roi:/app/resources/roi" \
          -v "$HOME/.config/eodag/eodag.yml:/root/.config/eodag/eodag.yml:ro" \
          vegetacion:test grass "$GRASS_MAPSET" --exec python3 src/calculo_vegetacion.py > "$LOGFILE" 2>&1
    RC=$?
    if [ $RC -eq 0 ] && grep -q "Resultado generado" "$LOGFILE"; then
      echo "    OK"
      OK=$((OK+1))
    else
      echo "    FALLO (rc=$RC) -> revisar $LOGFILE"
      FAIL=$((FAIL+1))
    fi
  done
done

echo ""
echo "=== BACKFILL VEGETACION TERMINADO: $OK ok, $FAIL fallidas, $SKIP salteadas (ya existian) de $TOTAL_RUNS ==="
