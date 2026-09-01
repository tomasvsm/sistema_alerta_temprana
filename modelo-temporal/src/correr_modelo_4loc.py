"""
Corre el modelo temporal (Aguirre/Otero) + índice de oviposición para las
4 localidades. Paso 2 del orquestador semanal -- corre después de
actualizar_clima_semanal.py, así que por defecto llega hasta hoy +
FORECAST_RANGE días (el pronóstico recién agregado al CSV de clima).

start_date se mantiene fijo en 2023-01-01 para las 4 localidades por igual
-- le da al modelo ~1.5 años de spin-up antes de que la ventana de 365 días
del índice de oviposición empiece a importar (ver
calcular_indice_oviposicion.py). No mover salvo que se rediscuta esto.
"""
import datetime
import sys

sys.path.insert(0, "src")
from config import Configuration
import run as run_mod
import calcular_indice_oviposicion as cio
import get_weather as gw

START_DATE = "2023-01-01"
LOCALIDADES = [
    ("1252", "villa_maria"),
    ("1271", "salsipuedes"),
    ("1300", "rio_cuarto"),
    ("1385", "cordoba"),
]


def main(end_date=None):
    if end_date is None:
        # extractForecastData usa daterange(hoy, hoy+FORECAST_RANGE) -- exclusivo
        # del extremo superior, asi que el ultimo dia REAL de pronostico es
        # hoy + FORECAST_RANGE - 1, no hoy + FORECAST_RANGE.
        end_date = (datetime.date.today() + datetime.timedelta(days=gw.FORECAST_RANGE - 1)).strftime("%Y-%m-%d")

    print(f"=== Corrida del modelo temporal -- hasta {end_date} ===\n")

    for gid, nombre in LOCALIDADES:
        configuration = Configuration("resources/1c.cfg")
        configuration.config_parser.set("location", "name", nombre)
        configuration.config_parser.set("simulation", "start_date", START_DATE)
        configuration.config_parser.set("simulation", "end_date", end_date)
        configuration.config_parser.set("breeding_site", "height", "10")
        configuration.config_parser.set("breeding_site", "amount", "1")
        cfg_path = f"/tmp/run_{nombre}.cfg"
        configuration.save(cfg_path)

        modelo_csv = f"output/2023_2026_{gid}_{nombre}_modelo.csv"
        run_mod.main(cfg_path, modelo_csv, engine="cpp")

        indice_csv = f"output/2023_2026_{gid}_{nombre}_indice_oviposicion.csv"
        cio.main(modelo_csv, indice_csv)
        print(f"=== {nombre} (gid {gid}) listo ===\n")


if __name__ == "__main__":
    end_date = sys.argv[1] if len(sys.argv) > 1 else None
    main(end_date)
