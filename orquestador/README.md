# orquestador

Automatización semanal del sistema de alerta temprana. No es un contenedor
propio: es un script del host (`scripts/run_semanal.sh`) que llama a los
demás servicios (`docker run`) en secuencia. Se eligió así, en vez de un
contenedor con cron adentro, porque es más simple de depurar: cada paso se
puede correr suelto a mano si algo falla.

## Qué hace cada corrida

El script dispara los miércoles, pero el corte de datos siempre queda
anclado al martes anterior (ver más abajo por qué):

1. **Clima** (`modelo-temporal`): borra la ventana de los últimos 21 días
   de cada localidad, re-descarga clima real confirmado (GDEX + IMERG) y
   agrega pronóstico CFS fresco de 14 días. Si la descarga falla a mitad
   de camino, restaura un resguardo en vez de dejar un hueco.
2. **Modelo temporal**: corre el modelo de
   [Aguirre et al., 2021](https://doi.org/10.1016/j.ecoinf.2021.101351) y
   el índice de oviposición para las 4 localidades, hasta hoy más 14 días
   proyectados gracias al pronóstico del paso anterior.
3. **Vegetación**: agrega la semana de Sentinel-2 más reciente confirmada
   por satélite en las 4 localidades, reintentando hasta 52 semanas atrás
   por nubosidad, igual que el backfill. Es puramente retrospectivo: la
   vegetación no se puede pronosticar.
4. **MCDA**: idoneidad espacial para la semana nueva.
5. **Índice de actividad**: idoneidad por oviposición, resultado final.

El índice de oviposición queda proyectado 14 días a futuro; el índice de
actividad final, que necesita vegetación real, se mantiene siempre
puramente retrospectivo.

Ningún paso aborta la cadena si falla: cada uno corre pase lo que pase con
los anteriores, y el resultado queda registrado en el log de esa corrida
(`logs/semanal_YYYY-MM-DD.log`), con un "CARTEL ROJO" al final si algo
salió mal.

## Cron

Instalado, miércoles a las 06:00, 09:00 y 12:00:

```
0 6 * * 3 /home/tomas/sistema_alerta_temprana/orquestador/scripts/run_semanal.sh >> /home/tomas/sistema_alerta_temprana/orquestador/logs/cron.log 2>&1
0 9 * * 3 /home/tomas/sistema_alerta_temprana/orquestador/scripts/run_semanal.sh >> /home/tomas/sistema_alerta_temprana/orquestador/logs/cron.log 2>&1
0 12 * * 3 /home/tomas/sistema_alerta_temprana/orquestador/scripts/run_semanal.sh >> /home/tomas/sistema_alerta_temprana/orquestador/logs/cron.log 2>&1
```

Dispara miércoles y no martes a propósito: el corte de datos sigue siendo
el martes, pero corriendo un día después ese martes ya es un día
calendario completo. Sentinel-2 puede tardar hasta 24 horas en publicarse
e IMERG cerca de 14; disparando el martes mismo a las 6am, esas fuentes
todavía no tenían el dato del día, así que en la práctica solo se
aprovechaban 6 de los 7 días de la ventana semanal.

Los tres horarios sirven como reintento: si la máquina estaba apagada o
sin internet a las 6, el de las 9 reintenta la cadena completa, y si ese
también falla, el de las 12 reintenta una vez más. `run_semanal.sh` tiene
un guard al principio que chequea `estado_ultima_corrida.json`: si la
corrida de esa semana ya salió sin errores, los llamados de las 9 o las 12
no hacen nada. Si las tres fallan, queda el cartel rojo en el log,
esperando que se corra a mano.

El script además toma un lock (`flock`) para que dos corridas no se pisen
si una corrida manual coincide con un reintento de cron: la segunda que
llega ve el lock tomado, imprime que ya hay una corrida en curso y sale
sin tocar nada.

Para ver o editar el cron: `crontab -l` / `crontab -e`. El crontab incluye
una línea `PATH=` explícita que agrega Anaconda: sin ella, cron corre con
un PATH mínimo que no lo incluye, y algunos pasos manuales que todavía se
invocan fuera de Docker necesitan ese intérprete.

## Notificaciones (ntfy.sh)

Al final de cada corrida real (no en el camino rápido de "ya estaba ok")
se manda un push a `ntfy.sh/aedes-alerta-temprana-6f3d6bc9`: uno de
"terminó OK" o uno de "falló en: <pasos>" con la ruta al log. Para
recibirlos en el celular hay que instalar la app ntfy (Android/iOS) y
suscribirse al topic `aedes-alerta-temprana-6f3d6bc9`. No hace falta
cuenta ni configuración del lado del servidor. El topic funciona como una
contraseña débil, así que si el repo se hace público conviene rotarlo
cambiando `NTFY_TOPIC` en `run_semanal.sh`.

## Corte semanal anclado al martes

`run_semanal.sh` calcula el martes más reciente (menor o igual a hoy) y se
lo pasa a `actualizar_clima_semanal.py` como fecha de referencia, sin
importar qué día de la semana se ejecute realmente. Esto cumple dos
funciones: en el disparo normal del miércoles, ese martes es ayer, un día
ya completo; y en una corrida tardía (si el cron no llegó a correr por
falta de energía o de internet, y se pone al día otro día de la semana a
mano o porque el cron reintentó más tarde), el corte sigue siendo ese
mismo martes, no el día real de ejecución, así la grilla semanal no
avanza de más solo por el momento en que se ejecutó.

## Correr a mano

No hace falta esperar al cron: se puede correr en cualquier momento,
tantas veces como haga falta, porque los pasos son idempotentes y
saltean lo ya hecho.

```bash
bash /home/tomas/sistema_alerta_temprana/orquestador/scripts/run_semanal.sh
```

Funciona desde cualquier directorio: el script calcula sus propias rutas.
