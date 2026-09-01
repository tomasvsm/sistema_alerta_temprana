"""
Actualización semanal de clima -- paso 1 del orquestador, corre cada martes.

Cada corrida:
  1. Guarda una copia de resguardo de cada CSV de localidad.
  2. Borra la ventana reciente (últimos REFRESH_WINDOW_DIAS días) -- ahí
     puede haber pronóstico de la corrida anterior cuya fecha real ya pasó,
     y necesita reemplazarse por dato confirmado.
  3. Re-descarga clima REAL (GDEX + IMERG) para esa ventana. Si la descarga
     o la extracción fallan para una localidad, se restaura el resguardo
     (la ventana borrada vuelve a como estaba, con el dato viejo) en vez de
     quedar vacía -- nunca se deja un hueco por un fallo externo transitorio.
  4. Descarga pronóstico CFS fresco (FORECAST_RANGE días desde hoy) y lo
     agrega directo al CSV principal de cada localidad -- run.py lee ese
     mismo archivo, así que el modelo "ve" el pronóstico como si fueran
     datos más, sin distinguir nada especial.

Con esto el índice de oviposición queda proyectado FORECAST_RANGE días a
futuro, y se autocorrige la semana siguiente cuando esos días dejan de ser
pronóstico y pasan a ser dato real.

El índice de actividad final (espacial) NO se proyecta -- eso lo maneja el
paso de vegetación del orquestador, que solo agrega semanas ya confirmadas
por satélite.

Bug real detectado 2026-08-31 (por eso el patrón de resguardo): GESDISC
(servidor de IMERG) se puso inalcanzable a mitad de una corrida de prueba
-- como en ese momento el script borraba antes de confirmar el reemplazo,
dejó un hueco de 3 semanas en las 4 localidades. Se reparó a mano y se
reescribió el script con este patrón antes de instalarlo en el cron.
"""
import datetime
import json
import os
import shutil
import sys

sys.path.insert(0, "src")
import get_weather as gw
import gdas_lib
from configparser import ConfigParser

REFRESH_WINDOW_DIAS = 21  # > cadencia semanal (7) + margen de latencia GDEX/IMERG
IMERG_LATENCIA_DIAS = 2   # IMERG "Late" tarda ~2 dias en publicar un dia dado
ESTADO_PATH = "logs/estado_clima_semanal.json"


def borrar_ventana_reciente(csv_path, cutoff_date):
    with open(csv_path) as f:
        lines = f.readlines()
    kept = []
    removidas = 0
    for line in lines:
        d = line.split(",")[0].strip()
        try:
            fecha = datetime.date.fromisoformat(d)
        except ValueError:
            kept.append(line)  # header u otra linea no-fecha
            continue
        if fecha >= cutoff_date:
            removidas += 1
            continue
        kept.append(line)
    with open(csv_path, "w") as f:
        f.writelines(kept)
    return removidas


def main(fecha_ref=None):
    # fecha_ref: "hoy" a los efectos de esta corrida. Por defecto la fecha
    # real del sistema (para pruebas manuales sueltas); el orquestador
    # semanal (run_semanal.sh) siempre pasa el martes mas reciente, para que
    # de vez en cuando se corra tarde (ej. jueves porque el martes no se
    # pudo) no corra la grilla completa una fecha nueva -- el corte semanal
    # siempre queda anclado al martes, sea cual sea el dia real de ejecucion.
    hoy = datetime.date.fromisoformat(fecha_ref) if fecha_ref else datetime.date.today()
    cutoff = hoy - datetime.timedelta(days=REFRESH_WINDOW_DIAS)
    fin_real = hoy - datetime.timedelta(days=IMERG_LATENCIA_DIAS)

    cfg = ConfigParser()
    cfg.read("resources/get_weather.cfg")
    localidades = cfg.sections()

    print(f"=== Actualizacion semanal de clima -- {hoy} ===")
    print(f"Ventana a refrescar: {cutoff} -> hoy")
    print(f"Dato REAL hasta:     {fin_real} (margen de latencia IMERG)")
    print(f"Pronostico:          hoy -> +{gw.FORECAST_RANGE} dias\n")

    estado = {"fecha_corrida": str(hoy), "localidades": {}}

    print("Guardando resguardo de cada CSV...")
    for loc in localidades:
        shutil.copy2(f"data/public/{loc}.csv", f"data/public/{loc}.csv.bak")

    for loc in localidades:
        removidas = borrar_ventana_reciente(f"data/public/{loc}.csv", cutoff)
        print(f"  {loc}: {removidas} filas borradas (ventana reciente)")

    print("\nDescargando clima real (GDEX + IMERG) para la ventana...")
    try:
        request_id = gdas_lib.submit(cutoff, fin_real)
        print(f"  GDEX request_id={request_id}, esperando...")
        status = gdas_lib.waitFor(request_id)
        if status != "Completed":
            raise RuntimeError(f"Pedido GDEX no se completo (status={status})")
        gdas_lib.download(request_id)
        gw.downloadDataFromIMERG(cutoff, fin_real, gw.IMERG_FOLDER)
        gdas_ok = True
    except Exception as e:
        print(f"  [ERROR] Descarga de clima real fallo: {e}")
        gdas_ok = False

    for loc in localidades:
        lat, lon = float(cfg.get(loc, "lat")), float(cfg.get(loc, "lon"))
        loc_estado = {"clima_real": "sin_intentar", "pronostico": "sin_intentar"}
        bak_path = f"data/public/{loc}.csv.bak"
        csv_path = f"data/public/{loc}.csv"

        exito = False
        if gdas_ok:
            try:
                gw.extractHistoricData(lat, lon, cutoff, fin_real, csv_path)
                loc_estado["clima_real"] = "ok"
                print(f"  {loc}: clima real re-extraido")
                exito = True
            except Exception as e:
                loc_estado["clima_real"] = f"error: {e} (se restauro resguardo)"
                print(f"  [ERROR] {loc}: extraccion de clima real fallo: {e}")
        else:
            loc_estado["clima_real"] = "error: descarga GDEX/IMERG fallo (se restauro resguardo)"

        if not exito:
            shutil.copy2(bak_path, csv_path)

        estado["localidades"][loc] = loc_estado

    print(f"\nDescargando pronostico CFS ({gw.FORECAST_RANGE} dias)...")
    try:
        gw.downloadForecast()
        forecast_ok = True
    except Exception as e:
        print(f"  [ERROR] Descarga de pronostico fallo: {e}")
        forecast_ok = False

    for loc in localidades:
        lat, lon = float(cfg.get(loc, "lat")), float(cfg.get(loc, "lon"))
        if not forecast_ok:
            estado["localidades"][loc]["pronostico"] = "error: descarga CFS fallo"
            continue
        try:
            gw.extractForecastData(lat, lon, f"data/public/{loc}.csv")
            with open(f"data/public/{loc}.forecast.csv") as f:
                forecast_lines = f.readlines()[1:]
            with open(f"data/public/{loc}.csv", "a") as f:
                f.writelines(forecast_lines)
            estado["localidades"][loc]["pronostico"] = "ok"
            print(f"  {loc}: pronostico agregado ({len(forecast_lines)} dias)")
        except Exception as e:
            estado["localidades"][loc]["pronostico"] = f"error: {e}"
            print(f"  [ERROR] {loc}: extraccion de pronostico fallo: {e}")

    for loc in localidades:
        bak_path = f"data/public/{loc}.csv.bak"
        if os.path.exists(bak_path):
            os.remove(bak_path)

    os.makedirs(os.path.dirname(ESTADO_PATH), exist_ok=True)
    with open(ESTADO_PATH, "w") as f:
        json.dump(estado, f, indent=2, ensure_ascii=False)

    hubo_error = any(
        "error" in v["clima_real"] or "error" in v["pronostico"]
        for v in estado["localidades"].values()
    )
    print(f"\n=== Actualizacion de clima terminada -- {'CON ERRORES' if hubo_error else 'OK'} ===")
    print(f"Estado guardado en {ESTADO_PATH}")


if __name__ == "__main__":
    fecha_ref = sys.argv[1] if len(sys.argv) > 1 else None
    main(fecha_ref)
