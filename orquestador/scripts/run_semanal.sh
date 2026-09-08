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

# Topic de ntfy.sh para las notificaciones de fin de corrida (push al
# celular, gratis, sin cuenta: instalar la app ntfy y suscribirse a este
# topic). No es un secreto fuerte -- cualquiera que lo adivine podria
# mandar notificaciones falsas -- pero alcanza para uso personal. Si el
# repo se hace publico alguna vez, rotarlo.
NTFY_TOPIC="aedes-alerta-temprana-6f3d6bc9"

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

# Evita que dos corridas (ej. un reintento de cron y una corrida a mano)
# se pisen al mismo tiempo -- pueden terminar escribiendo el mismo mapset
# de GRASS o los mismos CSV de clima a la vez. flock (no un simple
# archivo PID) porque se libera solo si el proceso muere de cualquier
# forma, sin dejar un lock trabado. `-n`: no espera, si ya esta tomado
# sale ya mismo (no tiene sentido hacer cola, el que esta corriendo ya
# va a terminar la cadena entera).
# Bug real 2026-09-08: una corrida a mano y el reintento de las 12hs
# corrieron a la vez, y el segundo alcanzo a borrar (sin llegar a
# reponer) los CSV de clima que el primero ya habia dejado bien -- hubo
# que restaurarlos a mano desde el .bak.
LOCKFILE="$LOGDIR/run_semanal.lock"
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "  Ya hay una corrida de run_semanal.sh en curso -- salgo sin hacer nada."
    exit 0
fi

# El cron llama a este script 3 veces los martes (6, 9 y 12hs) para poder
# reintentar si la maquina estaba apagada o sin internet a las 6 -- pero
# si la corrida de esta semana YA salio bien antes, no hace falta repetir
# toda la cadena (serie redescargas/recomputos inutiles). Se pisa
# igual si la corrida anterior tuvo error, para que el reintento de las
# 9/12 la vuelva a intentar entera.
ESTADO_JSON="$LOGDIR/estado_ultima_corrida.json"
if [ -f "$ESTADO_JSON" ]; then
    ya_ok="$(python3 -c "
import json
try:
    e = json.load(open('$ESTADO_JSON'))
    print('si' if e.get('fecha_ref') == '$FECHA_REF' and not e.get('hubo_error', True) else 'no')
except Exception:
    print('no')
")"
    if [ "$ya_ok" = "si" ]; then
        echo "  La corrida de esta semana (ref $FECHA_REF) ya se completo sin errores."
        echo "  Nada que hacer -- este llamado es uno de los reintentos programados (9/12hs)."
        exit 0
    fi
fi

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
  modelo-temporal:test python3 src/correr_modelo_4loc.py $FECHA_REF
"

# --- Paso 3: vegetacion de la semana actual (host, necesita GRASS) ------
correr_paso "vegetacion" bash "$REPO_ROOT/espacializacion/scripts/run_veg_backfill.sh" "$FECHA_REF"

# --- Paso 4: MCDA (idoneidad espacial), host -----------------------------
correr_paso "mcda" bash "$REPO_ROOT/espacializacion/scripts/correr_mcda_todas.sh"

# --- Paso 5: indice de actividad final (idoneidad x oviposicion) --------
correr_paso "indice_actividad" sg docker -c "
docker run --rm \
  -v $REPO_ROOT/espacializacion/data/vegetacion:/app/data/vegetacion:ro \
  -v $REPO_ROOT/espacializacion/output:/app/output \
  -v $REPO_ROOT/modelo-temporal/output:/app/../modelo-temporal/output:ro \
  geoprocesos:test python3 src/calculo_indice_actividad.py $FECHA_REF
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

# --- Estado consolidado, para que el dashboard lo lea -----------------------
# Se pisa en cada corrida (siempre representa la corrida MAS RECIENTE, no un
# historico) -- el dashboard solo necesita saber si HOY hay que mostrar el
# cartel rojo o no. El log fechado (arriba) queda como historial para debug
# manual, este JSON es la unica fuente que el dashboard deberia leer.
PASO_clima="${ESTADO[clima]:-no_corrido}" \
PASO_modelo_temporal="${ESTADO[modelo_temporal]:-no_corrido}" \
PASO_vegetacion="${ESTADO[vegetacion]:-no_corrido}" \
PASO_mcda="${ESTADO[mcda]:-no_corrido}" \
PASO_indice_actividad="${ESTADO[indice_actividad]:-no_corrido}" \
FECHA_CORRIDA="$FECHA_CORRIDA" FECHA_REF="$FECHA_REF" HUBO_ERROR="$hubo_error" LOGFILE="$LOGFILE" \
ESTADO_JSON="$ESTADO_JSON" \
python3 - <<'PYEOF'
import json, os

pasos = {
    nombre: os.environ[f"PASO_{nombre}"]
    for nombre in ["clima", "modelo_temporal", "vegetacion", "mcda", "indice_actividad"]
}
estado = {
    "fecha_corrida": os.environ["FECHA_CORRIDA"],
    "fecha_ref": os.environ["FECHA_REF"],
    "hubo_error": os.environ["HUBO_ERROR"] == "1",
    "pasos": pasos,
    "log": os.environ["LOGFILE"],
}
with open(os.environ["ESTADO_JSON"], "w") as f:
    json.dump(estado, f, indent=2, ensure_ascii=False)
print(f"  Estado consolidado: {os.environ['ESTADO_JSON']}")
PYEOF

# --- Notificacion push (ntfy.sh) ------------------------------------------
# Un aviso por corrida REAL (no en el fast-path de "ya estaba ok" de mas
# arriba, para no mandar 3 avisos identicos los martes que salen bien a
# las 6am). --max-time corta la notificacion si ntfy.sh esta caido, para
# que nunca cuelgue el cron por esto.
if [ "$hubo_error" -eq 1 ]; then
    pasos_fallidos="$(for p in clima modelo_temporal vegetacion mcda indice_actividad; do
        [[ "${ESTADO[$p]:-}" == ERROR* ]] && echo -n "$p "
    done)"
    curl -s --max-time 10 \
        -H "Title: Alerta temprana Aedes -- corrida con errores" \
        -H "Priority: high" \
        -H "Tags: warning" \
        -d "Corrida $FECHA_CORRIDA (ref $FECHA_REF) fallo en: $pasos_fallidos. Log: $LOGFILE" \
        "ntfy.sh/$NTFY_TOPIC" > /dev/null || true
else
    curl -s --max-time 10 \
        -H "Title: Alerta temprana Aedes -- corrida OK" \
        -H "Tags: white_check_mark" \
        -d "Corrida $FECHA_CORRIDA (ref $FECHA_REF) termino sin errores: clima, modelo_temporal, vegetacion, mcda, indice_actividad." \
        "ntfy.sh/$NTFY_TOPIC" > /dev/null || true
fi
