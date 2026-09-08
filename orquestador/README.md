# orquestador

Automatización semanal del sistema de alerta temprana. No es un contenedor
propio: es un script del host (`scripts/run_semanal.sh`) que llama a los
demás servicios (`docker run`) en secuencia. Se eligió así, en vez de un
contenedor con cron adentro, por ser más simple de depurar: cada paso se
puede correr suelto a mano si algo falla.

## Qué hace cada corrida (dispara los miércoles, corte de datos hasta el martes)

1. **Clima** (`modelo-temporal`): borra la ventana de los últimos 21 días
   de cada localidad, re-descarga clima REAL confirmado (GDEX + IMERG) y
   agrega pronóstico CFS fresco de 14 días. Si la descarga falla a mitad de
   camino, restaura un resguardo: nunca deja un hueco por un corte externo
   transitorio.
2. **Modelo temporal**: corre el modelo Aguirre/Otero + índice de
   oviposición para las 4 localidades, hasta hoy + 14 días (proyectado,
   gracias al pronóstico del paso 1).
3. **Vegetación**: agrega la semana de Sentinel-2 más reciente confirmada
   por satélite, en las 4 localidades (reintenta hasta 52 semanas atrás por
   nubosidad, igual que el backfill). Puramente retrospectivo: la
   vegetación no se puede pronosticar.
4. **MCDA**: idoneidad espacial para la semana nueva.
5. **Índice de actividad**: idoneidad × oviposición, resultado final.

El índice de oviposición queda proyectado 14 días a futuro; el índice de
actividad final (que necesita vegetación real) se mantiene siempre
puramente retrospectivo.

Ningún paso aborta la cadena si falla: cada uno corre pase lo que pase con
los anteriores, y el resultado queda en el log de esa corrida
(`logs/semanal_YYYY-MM-DD.log`) con un "CARTEL ROJO" al final si algo salió
mal. Esto es la base para que el futuro dashboard muestre el estado por
localidad/paso.

## Cron

**Instalado** (actualizado 2026-09-08), miércoles a las 06:00, 09:00 y 12:00:

```
0 6 * * 3 /home/tomas/sistema_alerta_temprana/orquestador/scripts/run_semanal.sh >> /home/tomas/sistema_alerta_temprana/orquestador/logs/cron.log 2>&1
0 9 * * 3 /home/tomas/sistema_alerta_temprana/orquestador/scripts/run_semanal.sh >> /home/tomas/sistema_alerta_temprana/orquestador/logs/cron.log 2>&1
0 12 * * 3 /home/tomas/sistema_alerta_temprana/orquestador/scripts/run_semanal.sh >> /home/tomas/sistema_alerta_temprana/orquestador/logs/cron.log 2>&1
```

Dispara miércoles, no martes, a propósito (cambiado 2026-09-08): el corte
de datos sigue siendo el martes (ver sección de abajo), pero corriendo un
día después ese martes ya es un día calendario completo. Corriendo el
martes mismo a las 6am, la vegetación (Sentinel-2, hasta 24h de latencia
de publicación) y el clima (IMERG, ~14h de latencia) perdían ese último
día porque todavía no existía o no estaba publicado -- en la práctica se
aprovechaban como máximo 6 de los 7 días de la ventana semanal.

Tres horarios en vez de uno: si la máquina estaba apagada o sin internet a
las 6, el de las 9 reintenta la cadena completa; si ese también falla, el
de las 12 reintenta una vez más. `run_semanal.sh` tiene un guard al
principio que chequea `estado_ultima_corrida.json`: si la corrida de esa
semana (mismo `fecha_ref`) ya salió sin errores, el llamado de las 9 o las
12 no hace nada (no vuelve a descargar/recalcular todo de nuevo). Si las
3 fallan, queda como estaba antes: cartel rojo en el log, esperando que
se corra a mano.

**Lock contra corridas simultáneas** (agregado 2026-09-08): si una corrida
a mano y un reintento de cron caen al mismo tiempo, `run_semanal.sh` tiene
un `flock` al principio -- el segundo que llega ve el lock tomado e
imprime "Ya hay una corrida en curso" y sale sin tocar nada. Bug real que
motivó esto: una corrida a mano y el reintento de las 12hs corrieron a la
vez, y el segundo alcanzó a borrar (sin llegar a reponer) los CSV de
clima que el primero ya había dejado bien -- hubo que restaurarlos a mano
desde el `.bak`.

Para verla o editarla: `crontab -l` / `crontab -e`.

**PATH explícito en el crontab** (agregado 2026-09-07): sin esto, cron corre
con un PATH mínimo (`/usr/bin:/bin`) que no incluye Anaconda, así que
`python3` resuelve al Python del sistema en vez del de Anaconda -- y el
paso de vegetación (`geopandas`, `eodag`) y el de MCDA fallarían con
`ModuleNotFoundError` en toda corrida disparada por cron (nunca se notó
antes porque cada prueba se hizo corriendo el script a mano desde una
terminal, donde `.bashrc` ya pone Anaconda primero en el PATH). Confirmado
con una simulación del entorno real de cron antes de instalar el fix. Este
riesgo ya no aplica a vegetación/capas-estáticas/MCDA (dockerizados el
2026-09-07): el Python de un contenedor no depende del PATH de quien lo
invoca. Se deja igual esta línea en el crontab como red de seguridad.

## Notificaciones (ntfy.sh)

Al final de cada corrida REAL (no en el fast-path de "ya estaba ok") se
manda un push a `ntfy.sh/aedes-alerta-temprana-6f3d6bc9` -- uno de
"terminó OK" o uno de "falló en: <pasos>" con el path al log. Para
recibirlos en el celular: instalar la app **ntfy** (Android/iOS) y
suscribirse al topic `aedes-alerta-temprana-6f3d6bc9`. No hace falta
cuenta ni configuración del lado del servidor, ntfy.sh es gratis y el
topic funciona como una contraseña débil (cualquiera que lo adivine
podría publicar ahí) -- si el repo se hace público alguna vez, rotarlo
(cambiar `NTFY_TOPIC` en `run_semanal.sh`).

## Corte semanal anclado al martes

`run_semanal.sh` calcula el martes más reciente (≤ hoy) y se lo pasa a
`actualizar_clima_semanal.py` como fecha de referencia -- sin importar qué
día de la semana se ejecute realmente. Dos usos de esto:

1. **Disparo normal (miércoles)**: "el martes más reciente" es ayer, un
   día calendario ya completo -- por esto se movió el disparo de martes a
   miércoles (ver sección Cron).
2. **Corridas tardías**: si el cron no llegó a correr (máquina apagada,
   sin internet) y se pone al día otro día de la semana (a mano, o porque
   el cron reintentó más tarde), el corte sigue siendo ESE martes, no el
   día real de ejecución. Así la grilla semanal no se corre de más solo
   por el timing de cuándo se ejecutó.

## Correr a mano (para probar o para ponerse al día vos mismo)

No hace falta esperar al cron: se puede correr en cualquier momento,
tantas veces como haga falta (los pasos son idempotentes, saltean lo ya
hecho):

```bash
bash /home/tomas/sistema_alerta_temprana/orquestador/scripts/run_semanal.sh
```

(funciona desde cualquier directorio: el script calcula sus propias rutas)

