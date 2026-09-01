#!/usr/bin/env python3
"""
calculo_indice_actividad.py
============================
Combina el índice de idoneidad semanal (MCDA) con el índice de oviposición
diario (modelo temporal de Aguirre) para generar el índice de actividad de
Aedes aegypti por semana y localidad:

  IdA_w(x,y) = (1/D) * Σ( idoneidad_w(x,y) * oviposicion_d )
  σ_w(x,y)   = sqrt( (1/D) * Σ( IdA_d(x,y) - IdA_w(x,y) )² )

Siendo:
  D            = 7 días
  idoneidad_w  = índice de idoneidad de hábitat para la semana w (MCDA)
  oviposicion_d = índice de oviposición del día d (con piso mínimo)
  IdA_d        = idoneidad_w * oviposicion_d  (valor diario integrado)
  IdA_w        = promedio de IdA_d sobre los 7 días
  σ_w          = desvío estándar intra-semanal

Lee el índice de oviposición diario directo de la salida de
modelo-temporal/src/calcular_indice_oviposicion.py (un CSV por localidad,
serie continua) -- no hace falta un paso intermedio como antes.

Uso:
  python3 calculo_indice_actividad.py
"""

import os
import re
import numpy as np
import pandas as pd
import rasterio


# =========================================================
# PARÁMETROS INTERNOS
# =========================================================

MCDA_DIR         = "output/MCDA"
OVIPOSICION_DIR  = "../modelo-temporal/output"
OUTPUT_DIR       = "output/indice_actividad"
OVIPOSICION_FLOOR = 0.1  # piso mínimo del índice de oviposición

GID_TO_NOMBRE = {
    "1252": "villa_maria",
    "1271": "salsipuedes",
    "1300": "rio_cuarto",
    "1385": "cordoba",
}


# =========================================================
# UTILIDADES
# =========================================================

def scan_mcda_files(mcda_dir):
    """Escanea MCDA_DIR y extrae gid y fecha_fin de cada archivo.
    Formato esperado: {end_date}_{gid}_MCDA.tif"""
    if not os.path.isdir(mcda_dir):
        raise FileNotFoundError(f"No se encontró el directorio MCDA: {mcda_dir}")

    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d+)_MCDA\.tif$")
    entries = []
    for fname in sorted(os.listdir(mcda_dir)):
        m = pattern.match(fname)
        if not m:
            continue
        entries.append({"end_date": m.group(1), "gid": m.group(2), "filename": fname})
    return entries


def load_oviposicion_csv(gid):
    """Carga el CSV diario de índice de oviposición para un gid."""
    nombre = GID_TO_NOMBRE.get(gid)
    if nombre is None:
        raise ValueError(f"gid desconocido: {gid} (agregar a GID_TO_NOMBRE)")

    candidates = [
        f for f in os.listdir(OVIPOSICION_DIR)
        if re.match(rf"^\d+_\d+_{gid}_{nombre}_indice_oviposicion\.csv$", f)
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No se encontró CSV de índice de oviposición para gid={gid} en {OVIPOSICION_DIR}"
        )
    df = pd.read_csv(os.path.join(OVIPOSICION_DIR, candidates[0]))
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def get_daily_oviposicion(df, end_date_str):
    """
    Extrae los 7 valores diarios del índice de oviposición para la semana
    que termina en end_date_str, con piso mínimo aplicado.

    Returns
    -------
    list[float] o None
        7 valores [d1..d7], o None si no hay datos para alguno de los días.
    """
    end_date = pd.to_datetime(end_date_str)
    week = df[(df["date"] > end_date - pd.Timedelta(days=7)) & (df["date"] <= end_date)]
    if len(week) < 7:
        return None
    valores = [max(float(v), OVIPOSICION_FLOOR) for v in week["indice_oviposicion"]]
    return valores


def compute_indice_actividad(mcda_arr, oviposicion_daily):
    """
    IdA_w(x,y) = (1/7) * Σ( idoneidad(x,y) * oviposicion_d )
    σ_w(x,y)   = sqrt( (1/7) * Σ( IdA_d(x,y) - IdA_w(x,y) )² )
    """
    idA_d_stack = np.stack([mcda_arr * ov for ov in oviposicion_daily], axis=0)  # (7,H,W)
    idA_w = np.nanmean(idA_d_stack, axis=0)
    diff_sq = (idA_d_stack - idA_w[np.newaxis, :, :]) ** 2
    sigma = np.sqrt(np.nanmean(diff_sq, axis=0))

    nodata_mask = np.isnan(mcda_arr)
    idA_w = np.where(nodata_mask, np.nan, idA_w)
    sigma = np.where(nodata_mask, np.nan, sigma)
    return idA_w.astype(np.float32), sigma.astype(np.float32)


def save_tiff(arr, ref_path, out_path):
    with rasterio.open(ref_path) as src:
        meta = src.meta.copy()
    meta.update({"dtype": "float32", "nodata": -9999, "count": 1})
    out_arr = np.where(np.isnan(arr), -9999, arr).astype(np.float32)
    with rasterio.open(out_path, "w", **meta) as dst:
        dst.write(out_arr, 1)


# =========================================================
# MAIN
# =========================================================

def main():
    print("\n=== ÍNDICE DE ACTIVIDAD DE AEDES AEGYPTI (idoneidad × oviposición) ===\n")
    print(f"  MCDA (idoneidad) dir:   {MCDA_DIR}")
    print(f"  Oviposición dir:        {OVIPOSICION_DIR}")
    print(f"  Salida:                 {OUTPUT_DIR}")
    print(f"  Piso oviposición:       {OVIPOSICION_FLOOR}\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    entries = scan_mcda_files(MCDA_DIR)
    if not entries:
        print("  [ERROR] No se encontraron archivos MCDA en el directorio.")
        return
    print(f"  Archivos MCDA encontrados: {len(entries)}")

    oviposicion_cache = {}
    n_ok, n_sin_ovip, n_err = 0, 0, 0

    for entry in entries:
        gid, end_date_str = entry["gid"], entry["end_date"]

        out_idA = os.path.join(OUTPUT_DIR, f"{end_date_str}_{gid}_indice_actividad.tif")
        out_sigma = os.path.join(OUTPUT_DIR, f"{end_date_str}_{gid}_sigma.tif")
        if os.path.exists(out_idA) and os.path.exists(out_sigma):
            n_ok += 1
            continue

        if gid not in oviposicion_cache:
            try:
                oviposicion_cache[gid] = load_oviposicion_csv(gid)
            except FileNotFoundError as e:
                print(f"  [ERROR] {e}")
                oviposicion_cache[gid] = None

        df_ovip = oviposicion_cache[gid]
        if df_ovip is None:
            n_err += 1
            continue

        oviposicion_daily = get_daily_oviposicion(df_ovip, end_date_str)
        if oviposicion_daily is None:
            print(f"  [AVISO] Sin oviposición completa para gid={gid} fecha={end_date_str}: saltando.")
            n_sin_ovip += 1
            continue

        mcda_path = os.path.join(MCDA_DIR, entry["filename"])
        try:
            with rasterio.open(mcda_path) as src:
                mcda_raw = src.read(1).astype(np.float32)
                nodata = src.nodata if src.nodata is not None else -9999
            mcda_arr = np.where(mcda_raw == nodata, np.nan, mcda_raw)

            idA_arr, sigma_arr = compute_indice_actividad(mcda_arr, oviposicion_daily)
            save_tiff(idA_arr, mcda_path, out_idA)
            save_tiff(sigma_arr, mcda_path, out_sigma)

            print(f"  ✔  {end_date_str} gid={gid}: "
                  f"IdA=[{np.nanmin(idA_arr):.3f}, {np.nanmax(idA_arr):.3f}]  "
                  f"σ=[{np.nanmin(sigma_arr):.3f}, {np.nanmax(sigma_arr):.3f}]")
            n_ok += 1
        except Exception as e:
            print(f"  ✘  Error gid={gid} fecha={end_date_str}: {e}")
            n_err += 1

    print("\n" + "=" * 55)
    print("  RESUMEN FINAL")
    print("=" * 55)
    print(f"  Procesados:          {n_ok}")
    print(f"  Sin oviposición:     {n_sin_ovip}")
    print(f"  Errores:             {n_err}")
    print(f"  TIFFs en:            {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
