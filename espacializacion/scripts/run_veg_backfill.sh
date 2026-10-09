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

# Un .gislock huerfano bloquea TODAS las corridas de GRASS hasta que alguien
# lo note y lo borre a mano -- paso real 2026-09-16, encontrado recien tras
# DOS semanas de fallos silenciosos (el .gislock quedo de una corrida del
# 2026-09-09 que se colgo o fue matada sin que GRASS alcanzara a liberarlo).
# El unico proceso que toca este mapset es el "docker run ... vegetacion:test"
# de mas abajo, siempre desde este mismo script -- si al arrancar no hay
# NINGUN contenedor vegetacion:test corriendo, cualquier .gislock que exista
# es necesariamente huerfano, no una corrida concurrente real (esa la evita
# igual el flock de run_semanal.sh, una capa mas arriba).
GISLOCK_HOST="/home/tomas/grassdata/posgar2007_4_cba/MCDA/.gislock"
if [ -f "$GISLOCK_HOST" ] && ! docker ps --format '{{.Image}}' | grep -qx 'vegetacion:test'; then
  echo "[AVISO] .gislock huerfano encontrado (sin contenedor vegetacion:test corriendo) -- se elimina antes de arrancar."
  rm -f "$GISLOCK_HOST"
fi

declare -A ROIS=(
  [cordoba]="resources/roi/roi_gid_1385_1000m.gpkg"
  [rio_cuarto]="resources/roi/roi_gid_1300_1000m.gpkg"
  [villa_maria]="resources/roi/roi_gid_1252_1000m.gpkg"
  [salsipuedes]="resources/roi/roi_gid_1271_1000m.gpkg"
)

# $1 (obligatorio): fecha de referencia de la corrida (el martes ancla que
# pasa run_semanal.sh). Antes tenia un default a $(date +%Y-%m-%d) -- una
# corrida manual sin pasar este argumento (ej. durante una recuperacion de
# incidente) tomaba la fecha real del sistema como limite superior en vez
# del martes ancla, generando una semana de vegetacion (y despues, via
# correr_mcda_todas.sh, de MCDA) de mas que el resto del pipeline nunca
# pidio -- paso real 2026-09-16, detectado recien el 2026-09-18 porque el
# dashboard mostraba una semana "futura" en idoneidad/vegetacion que no
# existia en indice de actividad. Ahora falla fuerte en vez de asumir nada.
if [ -z "${1:-}" ]; then
  echo "[ERROR] falta el argumento FECHA_REF (fecha de referencia, ej. 2026-09-15)." >&2
  echo "        no se usa la fecha del sistema como default a proposito:" >&2
  echo "        una corrida manual sin este argumento genera una semana de" >&2
  echo "        vegetacion de mas si no se corre justo el martes ancla." >&2
  exit 1
fi
FECHA_REF="$1"

FECHAS=()
cur="2025-01-07"
# El limite se compara contra FECHA_FIN (=cur+7), no contra FECHA (=cur):
# FECHA_REF es el martes mas reciente YA CONCLUIDO (lo resuelve run_semanal.sh
# antes de llamar a este script). Comparar directo contra FECHA hacia que la
# ultima semana de la grilla pidiera FECHA_FIN=FECHA_REF+7, es decir imagenes
# de dias que todavia no habian pasado el dia de la corrida -- por eso esa
# semana SIEMPRE caia en el fallback "retrocedio 1 semana" (bug real
# encontrado 2026-10-09: confirmado con el log interno de
# villa_maria_2026-09-30_2026-10-06_vegetacion, que pedia explicitamente
# la ventana 2026-09-30 -> 2026-10-06 corriendo el propio 2026-09-30).
end="$(date -d "$FECHA_REF - 7 days" +%Y-%m-%d)"
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
    # calculo_vegetacion.py etiqueta la carpeta con label_start=ref_date-6 (no
    # ref_date-7): como aca ref_date=FECHA_FIN, el nombre real que genera es
    # FECHA+1 .. FECHA_FIN (6 dias de diferencia), no FECHA .. FECHA_FIN como
    # asumia este chequeo. Ese desajuste (bug real encontrado 2026-10-09, tras
    # el fix de off-by-one aplicado a get_date_window en calculo_vegetacion.py
    # el 2026-09-08/09) hacia que el chequeo de "ya existe" nunca coincidiera
    # para ninguna semana procesada desde esa fecha, y cada corrida del
    # backfill reprocesaba TODAS esas semanas en las 4 localidades de nuevo.
    LABEL_START="$(date -d "$FECHA + 1 day" +%Y-%m-%d)"
    OUTDIR="data/vegetacion/${LOCALIDAD}_${LABEL_START}_${FECHA_FIN}_vegetacion"
    OUTNAME="${LOCALIDAD}_${LABEL_START}_${FECHA_FIN}_vegetacion"
    RESULTADO_FINAL="$OUTDIR/outputs/final/${OUTNAME}_NDVI_cat_100m.tif"
    # Semanas procesadas ANTES del fix de off-by-one en get_date_window
    # (2026-09-08/09) quedaron guardadas con la convencion vieja (FECHA
    # .. FECHA_FIN, 7 dias). Se chequean ambas para no reprocesar esas
    # semanas ya validas solo por el cambio de convencion de nombres.
    OUTDIR_VIEJO="data/vegetacion/${LOCALIDAD}_${FECHA}_${FECHA_FIN}_vegetacion"
    OUTNAME_VIEJO="${LOCALIDAD}_${FECHA}_${FECHA_FIN}_vegetacion"
    RESULTADO_FINAL_VIEJO="$OUTDIR_VIEJO/outputs/final/${OUTNAME_VIEJO}_NDVI_cat_100m.tif"

    # Chequear el ARCHIVO FINAL, no la carpeta: calculo_vegetacion.py crea
    # el arbol de carpetas (ensure_dirs) antes de descargar/procesar nada,
    # asi que si el proceso se cae a mitad de camino (red, GRASS colgado,
    # OOM-kill) la carpeta queda creada pero incompleta -- con el chequeo
    # viejo (solo "existe la carpeta") esa semana quedaba salteada para
    # siempre en todos los reintentos futuros, sin volver a procesarse.
    if [ -f "$RESULTADO_FINAL" ] || [ -f "$RESULTADO_FINAL_VIEJO" ]; then
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

# Contaba $FAIL pero nunca devolvia un codigo de salida distinto de 0 --
# run_semanal.sh (correr_paso) SI revisa el exit code para decidir si el
# paso "vegetacion" salio ok o con error, asi que con esto ausente una
# semana entera de fallos (las 4 localidades) quedaba reportada como "ok"
# en la notificacion final. Bug real encontrado 2026-09-16: un .gislock
# huerfano de una corrida anterior bloqueo vegetacion silenciosamente
# durante dos semanas seguidas sin que la notificacion lo mostrara.
if [ $FAIL -gt 0 ]; then
  exit 1
fi
