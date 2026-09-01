#!/bin/bash
# Corrida operativa semanal del sistema de alerta temprana.
# Pensado para cron, todos los martes (ver orquestador/README.md para la
# linea de crontab). No pide nada por input -- todas las fechas se calculan
# solas a partir de "hoy".
#
# Filosofia: NUNCA aborta la cadena entera por un paso que falla -- cada
# paso corre pase lo que pase con los anteriores, y el resultado (ok/error)
# de cada uno queda registrado en el log de esta corrida. Preferimos una
# salida parcial a ninguna salida (ver charla del 2026-08-31: "lo
# prioritario es que siempre haya una salida... si hay un error se
# notifique con un cartel rojo").
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FECHA_CORRIDA="$(date +%Y-%m-%d)"
LOGDIR="$REPO_ROOT/orquestador/logs"
LOGFILE="$LOGDIR/semanal_${FECHA_CORRIDA}.log"
mkdir -p "$LOGDIR"

exec > >(tee -a "$LOGFILE") 2>&1

# Corte semanal siempre anclado al martes mas reciente (<=hoy), sea cual sea
# el dia real en que esto se ejecute -- si un martes no se pudo correr y se
# pone al dia el jueves, el sistema se comporta como si fuera la corrida
# normal de ese martes, no avanza la grilla de mas. `date +%u`: 1=lunes,
# 2=martes, ..., 7=domingo.
DOW="$(date +%u)"
DIAS_DESDE_MARTES=$(( (DOW - 2 + 7) % 7 ))
FECHA_REF="$(date -d "$FECHA_CORRIDA - $DIAS_DESDE_MARTES days" +%Y-%m-%d)"

echo "======================================================="
echo "  CORRIDA SEMANAL -- $FECHA_CORRIDA (referencia: martes $FECHA_REF)"
echo "======================================================="

declare -A ESTADO

correr_paso() {
    local nombre="$1"
    shift
    echo ""
    echo "--- [$nombre] ---"
    if "$@"; then
        ESTADO["$nombre"]="ok"
        echo "--- [$nombre] OK ---"
    else
        ESTADO["$nombre"]="ERROR (rc=$?)"
        echo "--- [$nombre] FALLO -- se sigue con el resto de la cadena ---"
    fi
}

# --- Paso 1: clima real + pronostico (14 dias) -------------------------
correr_paso "clima" sg docker -c "
docker run --rm \
  -v $REPO_ROOT/modelo-temporal/data:/app/data \
  -v $REPO_ROOT/modelo-temporal/output:/app/output \
  -v $REPO_ROOT/modelo-temporal/logs:/app/logs \
  -v $REPO_ROOT/modelo-temporal/resources/passwords.cfg:/app/resources/passwords.cfg:ro \
  modelo-temporal:test python3 src/actualizar_clima_semanal.py $FECHA_REF
"

# --- Paso 2: modelo temporal + indice de oviposicion --------------------
correr_paso "modelo_temporal" sg docker -c "
docker run --rm \
  -v $REPO_ROOT/modelo-temporal/data:/app/data \
  -v $REPO_ROOT/modelo-temporal/output:/app/output \
  modelo-temporal:test python3 src/correr_modelo_4loc.py
"

# --- Paso 3: vegetacion de la semana actual (host, necesita GRASS) ------
correr_paso "vegetacion" bash "$REPO_ROOT/espacializacion/scripts/run_veg_backfill.sh"

# --- Paso 4: MCDA (idoneidad espacial), host -----------------------------
correr_paso "mcda" bash "$REPO_ROOT/espacializacion/scripts/correr_mcda_todas.sh"

# --- Paso 5: indice de actividad final (idoneidad x oviposicion) --------
correr_paso "indice_actividad" sg docker -c "
docker run --rm \
  -v $REPO_ROOT/espacializacion/data/vegetacion:/app/data/vegetacion:ro \
  -v $REPO_ROOT/espacializacion/output:/app/output \
  -v $REPO_ROOT/modelo-temporal/output:/app/../modelo-temporal/output:ro \
  geoprocesos:test python3 src/calculo_indice_actividad.py
"

# --- Resumen final --------------------------------------------------------
echo ""
echo "======================================================="
echo "  RESUMEN -- $FECHA_CORRIDA"
echo "======================================================="
hubo_error=0
for paso in clima modelo_temporal vegetacion mcda indice_actividad; do
    printf "  %-18s %s\n" "$paso" "${ESTADO[$paso]:-no_corrido}"
    [[ "${ESTADO[$paso]:-}" == ERROR* ]] && hubo_error=1
done

if [ "$hubo_error" -eq 1 ]; then
    echo ""
    echo "  [!] CARTEL ROJO -- al menos un paso fallo esta semana. Ver arriba."
fi

echo ""
echo "  Log completo: $LOGFILE"
