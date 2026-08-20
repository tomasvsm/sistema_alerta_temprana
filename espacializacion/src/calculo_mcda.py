# Para correr desde terminal:
# python3 /home/tomas/Desktop/Doctorado/espacializacion/calculo_mcda.py

"""
mcda_pipeline.py
================
Combina las variables de vegetación (semanales) con las variables
estáticas (construcciones, población, NBI) mediante álgebra de bandas
ponderada para generar un índice MCDA por semana y localidad.

Fórmula:
    MCDA = (W_VEG   * NDVI_cat)
         + (W_POB   * People_100)
         + (W_NBI   * NBI_100)
         + (W_BUILD * Buildings_cat_100m)

Parámetros configurables:
    VEG_BASE_DIR     Directorio raíz de resultados de vegetación
    EST_BASE_DIR     Directorio raíz de resultados de variables estáticas
    OUTPUT_DIR       Directorio de salida de los rasters MCDA
    W_VEG            Peso de vegetación
    W_POB            Peso de población
    W_NBI            Peso de NBI
    W_BUILD          Peso de construcciones

Uso:
    python3 calculo_mcda.py
"""

import os
import re
import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling
from datetime import datetime


# =========================================================
# PARÁMETROS INTERNOS
# =========================================================

VEG_BASE_DIR = "data/vegetacion"
EST_BASE_DIR = "estaticas"
OUTPUT_DIR   = "output/MCDA"

# Coeficientes MCDA — deben sumar 1.0
W_VEG   = 0.550
W_POB   = 0.240
W_NBI   = 0.095
W_BUILD = 0.095


# =========================================================
# UTILIDADES
# =========================================================

def get_veg_folders(gid):
    """
    Busca en VEG_BASE_DIR todas las carpetas de vegetación válidas
    para el gid dado. Excluye las que terminan en _cloud.

    Parameters
    ----------
    gid : str

    Returns
    -------
    list[dict]
        Lista de dicts con claves: folder_name, start_date, end_date, ndvi_path
    """
    if not os.path.isdir(VEG_BASE_DIR):
        raise FileNotFoundError(f"No se encontró el directorio: {VEG_BASE_DIR}")

    pattern = re.compile(
        rf"^{re.escape(gid)}_(\d{{4}}-\d{{2}}-\d{{2}})_(\d{{4}}-\d{{2}}-\d{{2}})_vegetacion$"
    )

    folders = []
    for name in sorted(os.listdir(VEG_BASE_DIR)):
        m = pattern.match(name)
        if not m:
            continue  # no coincide con el gid o termina en _cloud

        start_date = m.group(1)
        end_date   = m.group(2)
        folder_path = os.path.join(VEG_BASE_DIR, name)

        # Construir ruta al NDVI categorizado
        ndvi_filename = f"{name}_NDVI_cat_100m.tif"
        ndvi_path = os.path.join(folder_path, "outputs", "final", ndvi_filename)

        if not os.path.exists(ndvi_path):
            print(f"  [AVISO] No se encontró NDVI en: {ndvi_path} — saltando.")
            continue

        folders.append({
            "folder_name": name,
            "start_date":  start_date,
            "end_date":    end_date,
            "ndvi_path":   ndvi_path,
        })

    return folders


def get_static_paths(gid):
    """
    Busca las rutas a los tres archivos de variables estáticas para el gid.

    Parameters
    ----------
    gid : str

    Returns
    -------
    dict[str, str]
        Claves: buildings, poblacion, nbi

    Raises
    ------
    FileNotFoundError
        Si alguno de los archivos no existe.
    """
    est_dir = os.path.join(EST_BASE_DIR, f"gid_{gid}_estaticas")

    paths = {
        "buildings": os.path.join(
            est_dir, "construcciones", f"gid_{gid}_estaticas_Buildings_cat_100m.tif"
        ),
        "poblacion": os.path.join(
            est_dir, "poblacion", f"gid_{gid}_estaticas_People_100m.tif"
        ),
        "nbi": os.path.join(
            est_dir, "socioeconomica", f"gid_{gid}_estaticas_NBI_100m.tif"
        ),
    }

    for key, path in paths.items():
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"No se encontró la variable estática '{key}' para gid={gid}:\n  {path}"
            )

    return paths


def read_raster(path):
    """
    Lee un raster GeoTIFF y retorna el array, transform, crs y nodata.

    Parameters
    ----------
    path : str

    Returns
    -------
    tuple[np.ndarray, affine.Affine, CRS, float]
    """
    with rasterio.open(path) as src:
        arr     = src.read(1).astype(np.float32)
        transform = src.transform
        crs     = src.crs
        nodata  = src.nodata if src.nodata is not None else -9999
    return arr, transform, crs, nodata


def align_to_reference(src_arr, src_transform, src_crs, src_nodata,
                        ref_shape, ref_transform, ref_crs):
    """
    Reproyecta y remuestrea src_arr a la grilla de referencia.

    Parameters
    ----------
    src_arr : np.ndarray
    src_transform : affine.Affine
    src_crs : CRS
    src_nodata : float
    ref_shape : tuple[int, int]
        (height, width) de la grilla de referencia.
    ref_transform : affine.Affine
    ref_crs : CRS

    Returns
    -------
    np.ndarray
        Array alineado a la grilla de referencia.
    """
    dst = np.full(ref_shape, fill_value=np.nan, dtype=np.float32)
    reproject(
        source=src_arr,
        destination=dst,
        src_transform=src_transform,
        src_crs=src_crs,
        src_nodata=src_nodata,
        dst_transform=ref_transform,
        dst_crs=ref_crs,
        dst_nodata=np.nan,
        resampling=Resampling.nearest
    )
    return dst


def compute_mcda(ndvi_arr, build_arr, pob_arr, nbi_arr):
    """
    Calcula el índice MCDA aplicando los pesos definidos.

    Píxeles con NoData en cualquier capa → NoData en el resultado.

    Parameters
    ----------
    ndvi_arr, build_arr, pob_arr, nbi_arr : np.ndarray
        Arrays alineados a la misma grilla, con np.nan en NoData.

    Returns
    -------
    np.ndarray
        Índice MCDA, np.nan donde alguna capa tiene NoData.
    """
    # Máscara de validez: todos los píxeles deben tener dato en todas las capas
    valid = (~np.isnan(ndvi_arr) &
             ~np.isnan(build_arr) &
             ~np.isnan(pob_arr)   &
             ~np.isnan(nbi_arr))

    mcda = np.where(
        valid,
        W_VEG   * ndvi_arr  +
        W_POB   * pob_arr   +
        W_NBI   * nbi_arr   +
        W_BUILD * build_arr,
        np.nan
    )
    return mcda


def save_mcda(mcda_arr, ref_transform, ref_crs, out_path):
    """
    Guarda el raster MCDA como GeoTIFF Float32.

    Parameters
    ----------
    mcda_arr : np.ndarray
    ref_transform : affine.Affine
    ref_crs : CRS
    out_path : str
    """
    meta = {
        "driver":    "GTiff",
        "dtype":     "float32",
        "nodata":    -9999,
        "count":     1,
        "height":    mcda_arr.shape[0],
        "width":     mcda_arr.shape[1],
        "transform": ref_transform,
        "crs":       ref_crs,
    }
    # Reemplazar nan por nodata antes de guardar
    out_arr = np.where(np.isnan(mcda_arr), -9999, mcda_arr).astype(np.float32)
    with rasterio.open(out_path, "w", **meta) as dst:
        dst.write(out_arr, 1)


# =========================================================
# PROCESAMIENTO POR LOCALIDAD
# =========================================================

def process_gid(gid):
    """
    Procesa todas las semanas de vegetación disponibles para el gid dado,
    generando un raster MCDA por semana.

    Parameters
    ----------
    gid : str

    Returns
    -------
    tuple[int, int]
        (n_procesadas, n_errores)
    """
    print(f"\n{'='*55}")
    print(f"  Procesando localidad gid={gid}")
    print(f"{'='*55}")

    # --- Variables estáticas ---
    try:
        static_paths = get_static_paths(gid)
    except FileNotFoundError as e:
        print(f"  [ERROR] Variables estáticas no encontradas: {e}")
        return 0, 0

    build_arr, build_transform, build_crs, build_nodata = \
        read_raster(static_paths["buildings"])
    pob_arr,   pob_transform,   pob_crs,   pob_nodata   = \
        read_raster(static_paths["poblacion"])
    nbi_arr,   nbi_transform,   nbi_crs,   nbi_nodata   = \
        read_raster(static_paths["nbi"])

    # Convertir nodata a nan en estáticas
    build_arr = np.where(build_arr == build_nodata, np.nan, build_arr)
    pob_arr   = np.where(pob_arr   == pob_nodata,   np.nan, pob_arr)
    nbi_arr   = np.where(nbi_arr   == nbi_nodata,   np.nan, nbi_arr)

    # --- Semanas de vegetación ---
    veg_folders = get_veg_folders(gid)
    if not veg_folders:
        print(f"  [AVISO] No se encontraron semanas de vegetación válidas para gid={gid}.")
        return 0, 0

    print(f"  Semanas válidas encontradas: {len(veg_folders)}")

    n_ok  = 0
    n_err = 0

    for veg in veg_folders:
        start = veg["start_date"]
        end   = veg["end_date"]
        print(f"\n  Semana: {start} → {end}")

        out_filename = f"{end}_{gid}_MCDA.tif"
        out_path = os.path.join(OUTPUT_DIR, out_filename)

        if os.path.exists(out_path):
            print(f"    ⏭  Ya existe, saltando: {out_filename}")
            n_ok += 1
            continue

        try:
            # Leer NDVI — se usa como grilla de referencia
            ndvi_arr, ndvi_transform, ndvi_crs, ndvi_nodata = \
                read_raster(veg["ndvi_path"])
            ndvi_arr = np.where(ndvi_arr == ndvi_nodata, np.nan, ndvi_arr)

            ref_shape     = ndvi_arr.shape
            ref_transform = ndvi_transform
            ref_crs       = ndvi_crs

            # Alinear estáticas a la grilla del NDVI
            build_aligned = align_to_reference(
                build_arr, build_transform, build_crs, build_nodata,
                ref_shape, ref_transform, ref_crs
            )
            pob_aligned = align_to_reference(
                pob_arr, pob_transform, pob_crs, pob_nodata,
                ref_shape, ref_transform, ref_crs
            )
            nbi_aligned = align_to_reference(
                nbi_arr, nbi_transform, nbi_crs, nbi_nodata,
                ref_shape, ref_transform, ref_crs
            )

            # Calcular MCDA
            mcda = compute_mcda(ndvi_arr, build_aligned, pob_aligned, nbi_aligned)

            # Guardar
            save_mcda(mcda, ref_transform, ref_crs, out_path)
            print(f"    ✔ Guardado: {out_filename}")
            n_ok += 1

        except Exception as e:
            print(f"    ✘ Error procesando {start}→{end}: {e}")
            n_err += 1

    return n_ok, n_err


# =========================================================
# MAIN
# =========================================================

def main():
    """
    Solicita uno o más GIDs al usuario y genera los rasters MCDA
    para todas las semanas de vegetación disponibles de cada localidad.

    El usuario puede ingresar múltiples GIDs separados por coma.
    """
    print("\n=== MCDA PIPELINE ===\n")
    print(f"  Vegetación:   {VEG_BASE_DIR}")
    print(f"  Estáticas:    {EST_BASE_DIR}")
    print(f"  Salida:       {OUTPUT_DIR}")
    print(f"\n  Pesos:")
    print(f"    Vegetación:     {W_VEG}")
    print(f"    Población:      {W_POB}")
    print(f"    NBI:            {W_NBI}")
    print(f"    Construcciones: {W_BUILD}")
    print(f"    Total:          {W_VEG + W_POB + W_NBI + W_BUILD:.3f}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    gids_input = input(
        "\nGID(s) de localidad a procesar (separados por coma, ej: 1252,1300): "
    ).strip()

    if not gids_input:
        raise ValueError("Debés ingresar al menos un GID.")

    gids = [g.strip() for g in gids_input.split(",") if g.strip()]

    total_ok  = 0
    total_err = 0

    for gid in gids:
        ok, err = process_gid(gid)
        total_ok  += ok
        total_err += err

    print("\n" + "=" * 55)
    print("  RESUMEN FINAL")
    print("=" * 55)
    print(f"\n  Localidades procesadas: {len(gids)}")
    print(f"  Semanas generadas:      {total_ok}")
    print(f"  Errores:                {total_err}")
    print(f"  Resultados en:          {OUTPUT_DIR}")


if __name__ == "__main__":
    main()