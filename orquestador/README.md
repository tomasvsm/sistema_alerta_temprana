# orquestador

Automatización semanal del sistema de alerta temprana. No es un contenedor
propio: es un script del host (`scripts/run_semanal.sh`) que llama a los
demás servicios (`docker run`) en secuencia. Se eligió así, en vez de un
contenedor con cron adentro, por ser más simple de depurar: cada paso se
puede correr suelto a mano si algo falla.

## Qué hace cada corrida (pensada para los martes)

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

**Instalado** (2026-09-01), martes a las 06:00:

```
0 6 * * 2 /home/tomas/sistema_alerta_temprana/orquestador/scripts/run_semanal.sh >> /home/tomas/sistema_alerta_temprana/orquestador/logs/cron.log 2>&1
```

Para verla o editarla: `crontab -l` / `crontab -e`.

## Corte semanal anclado al martes

`run_semanal.sh` calcula el martes más reciente (≤ hoy) y se lo pasa a
`actualizar_clima_semanal.py` como fecha de referencia. Esto es intencional:
si el cron no llegó a correr un martes (máquina apagada, sin internet, etc.)
y se pone al día otro día de la semana (a mano o porque el cron reintentó
más tarde), el corte usado para la ventana de clima sigue siendo ESE
martes, no el día real de ejecución. Así la grilla semanal no se corre de
más solo por el timing de cuándo se ejecutó.

## Correr a mano (para probar o para ponerse al día vos mismo)

No hace falta esperar al cron: se puede correr en cualquier momento,
tantas veces como haga falta (los pasos son idempotentes, saltean lo ya
hecho):

```bash
bash /home/tomas/sistema_alerta_temprana/orquestador/scripts/run_semanal.sh
```

(funciona desde cualquier directorio: el script calcula sus propias rutas)

## Pendiente

- Definir si el "cartel rojo" del log también dispara algo más activo
  (mail, Slack) o si por ahora alcanza con quedar en el log para que el
  dashboard lo lea.
