# Para correr desde terminal:
# grass /home/tomas/grassdata/posgar2007_4_cba/MCDA --exec python3 /home/tomas/Desktop/Doctorado/espacializacion/variables_MCDA.py

"""
static_variables_pipeline.py
=============================
Pipeline para calcular variables estáticas del MCDA para una localidad dada:

  1. Construcciones (altura): procesado enteramente con rasterio/numpy/geopandas:
       - Detecta automáticamente si el DEM 5m cubre el ROI:
           * Si cubre → DEM 5m - FABDEM remuestrado a 5m (alta resolución)
           * Si no    → DEM 30m - FABDEM 30m directamente
       - Recorta DEM y FABDEM al ROI con rasterio
       - Calcula altura = DEM - FABDEM, negativos → 0
       - Recorta Open Buildings al ROI con geopandas (sin pasar por GRASS,
         evitando problemas de topología con polígonos solapados)
       - Extrae altura máxima por polígono con rasterio + numpy
       - Rasteriza la altura máxima por polígono
       - Exporta la altura sin categorizar (metros) a resolución nativa
       - Categoriza y exporta a resolución nativa y a 100m

  2. Población humana (WorldPop): procesado con GRASS:
       - Importa densidad poblacional 100m
       - Exporta la densidad sin categorizar a 100m
       - Categoriza y exporta a 100m

  3. Variable socioeconómica (NBI): procesado con GRASS:
       - Importa radios censales NBI
       - Rasteriza a 100m
       - Exporta la proporción NBI sin categorizar a 100m
       - Categoriza y exporta a 100m

Parámetros configurables:
    OUTPUT_BASE                  Directorio raíz de salida
    ROI_CACHE_DIR                Directorio con GPKGs de ROI precalculados
    PATH_DEM_5m                  DEM 5m (cobertura parcial: sierras de Córdoba)
    PATH_DEM_30m                 DEM 30m (cobertura provincial completa)
    PATH_FABDEM                  FABDEM 30m (mosaico de tiles)
    PATH_OPEN_BUILDINGS_TILES    Lista de tiles de Open Buildings
    PATH_WORLDPOP                WorldPop 100m
    PATH_NBI                     NBI radios censales GPKG

Uso:
    grass <ruta/location/mapset> --exec python3 variables_MCDA.py
"""

import os
import re
import json
import subprocess
from datetime import datetime

import numpy as np
import geopandas as gpd
import rasterio
from rasterio.mask import mask as rio_mask
from rasterio.features import rasterize as rio_rasterize
from rasterio.enums import Resampling
from rasterio.warp import reproject, calculate_default_transform
import grass.script as gs
import pandas as pd



# =========================================================
# PARÁMETROS INTERNOS
# =========================================================

OUTPUT_BASE   = "estaticas"
ROI_CACHE_DIR = "resources/roi"

PATH_DEM_5m  = "/home/tomas/gisdata/GIS_MCDA/dem_5m_cba_ext.tif"
PATH_DEM_30m = "/home/tomas/gisdata/GIS_MCDA/dem_ign_v2_cba_ext.tif"
PATH_FABDEM  = "/home/tomas/gisdata/GIS_MCDA/FABDEM_S31-34_W063-65.tif"

PATH_OPEN_BUILDINGS_TILES = [
    "/home/tomas/gisdata/GIS_MCDA/open_buildings/943_buildings.gpkg",
    "/home/tomas/gisdata/GIS_MCDA/open_buildings/95d_buildings.gpkg",
]

PATH_WORLDPOP = "/home/tomas/gisdata/GIS_MCDA/population/worlpop/unconstrained_100_2020_UNadj/arg_ppp_2020_UNadj.tif"
PATH_NBI      = "/home/tomas/gisdata/GIS_MCDA/socioeconomic/NBI_radios_2022/NBI_radios_cba_2022.gpkg"


# =========================================================
# UTILIDADES
# =========================================================

def sanitize_name(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def map_exists(map_name, map_type="raster"):
    try:
        maps = gs.read_command(
            "g.list", type=map_type, pattern=map_name, quiet=True
        ).strip()
        return map_name in maps.split()
    except Exception:
        return False


def get_gpkg_extent(gpkg_path):
    """Obtiene el extent (minx, miny, maxx, maxy) de un GPKG via ogrinfo."""
    result = subprocess.run(
        ["ogrinfo", "-al", "-so", gpkg_path],
        capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        if line.startswith("Extent:"):
            parts = line.replace("Extent:", "").replace("(", "").replace(")", "").split("-")
            minx, miny = [float(v.strip()) for v in parts[0].split(",")]
            maxx, maxy = [float(v.strip()) for v in parts[1].split(",")]
            return minx, miny, maxx, maxy
    raise ValueError(f"No se pudo obtener el extent de {gpkg_path}")


def get_raster_extent(raster_path):
    """Obtiene el extent de un GeoTIFF via gdalinfo."""
    result = subprocess.run(
        ["gdalinfo", "-json", raster_path],
        capture_output=True, text=True
    )
    info = json.loads(result.stdout)
    coords = info["cornerCoordinates"]
    return (
        coords["lowerLeft"][0],
        coords["lowerLeft"][1],
        coords["upperRight"][0],
        coords["upperRight"][1],
    )


def extents_overlap(ext1, ext2):
    minx1, miny1, maxx1, maxy1 = ext1
    minx2, miny2, maxx2, maxy2 = ext2
    return not (maxx1 < minx2 or maxx2 < minx1 or
                maxy1 < miny2 or maxy2 < miny1)


def dem_covers_roi(dem_path, roi_path):
    """Verifica si el DEM cubre completamente el bounding box del ROI."""
    try:
        gdf = gpd.read_file(roi_path)
        minx, miny, maxx, maxy = gdf.total_bounds
        dem_minx, dem_miny, dem_maxx, dem_maxy = get_raster_extent(dem_path)
        return (dem_minx <= minx and dem_maxx >= maxx and
                dem_miny <= miny and dem_maxy >= maxy)
    except Exception as e:
        print(f"  [AVISO] No se pudo verificar cobertura del DEM 5m: {e}")
        return False


def get_tiles_for_roi(roi_path):
    """Detecta qué tiles de Open Buildings cubren el ROI."""
    gdf = gpd.read_file(roi_path)
    roi_ext = tuple(gdf.total_bounds)
    tiles = []
    for tile_path in PATH_OPEN_BUILDINGS_TILES:
        tile_ext = get_gpkg_extent(tile_path)
        if extents_overlap(roi_ext, tile_ext):
            tiles.append(tile_path)
            print(f"    ✔ Tile relevante: {os.path.basename(tile_path)}")
    return tiles


def ensure_dirs(base_dir):
    paths = {
        "base":           base_dir,
        "construcciones": os.path.join(base_dir, "construcciones"),
        "poblacion":      os.path.join(base_dir, "poblacion"),
        "socioeconomica": os.path.join(base_dir, "socioeconomica"),
    }
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
    return paths


def get_roi_path(gid):
    if not os.path.isdir(ROI_CACHE_DIR):
        raise FileNotFoundError(f"No se encontró ROI cache: {ROI_CACHE_DIR}")
    candidates = [
        f for f in os.listdir(ROI_CACHE_DIR)
        if f.startswith(f"roi_gid_{gid}_") and f.endswith(".gpkg")
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No se encontró ROI para gid={gid} en {ROI_CACHE_DIR}\n"
            f"  Primero generalo con el pipeline de vegetación."
        )
    if len(candidates) > 1:
        print(f"  [AVISO] Múltiples ROIs para gid={gid}, usando: {candidates[0]}")
    return os.path.join(ROI_CACHE_DIR, candidates[0])


# =========================================================
# LOG
# =========================================================

def write_log(paths, gid, layers_imported, layers_reused, use_5m_dem, success):
    log_path = os.path.join(paths["base"], f"gid_{gid}_estaticas_procesamiento.txt")
    lines = []
    lines.append("=" * 60)
    lines.append("  VARIABLES ESTÁTICAS MCDA: LOG DE PROCESAMIENTO")
    lines.append("=" * 60)
    lines.append(f"  Localidad (gid):  {gid}")
    lines.append(f"  Fecha de log:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  DEM utilizado:    {'5m (alta resolución)' if use_5m_dem else '30m (cobertura provincial)'}")
    lines.append(f"  Resultado:        {'EXITOSO' if success else 'FALLIDO'}")
    lines.append("")
    lines.append("-" * 60)
    lines.append("  CAPAS IMPORTADAS A GRASS EN ESTA CORRIDA")
    lines.append("-" * 60)
    if layers_imported:
        for l in layers_imported:
            lines.append(f"  {l}")
    else:
        lines.append("  (ninguna: todas reutilizadas)")
    lines.append("")
    lines.append("-" * 60)
    lines.append("  CAPAS REUTILIZADAS DEL MAPSET")
    lines.append("-" * 60)
    if layers_reused:
        for l in layers_reused:
            lines.append(f"  {l}")
    else:
        lines.append("  (ninguna)")
    lines.append("")
    lines.append("-" * 60)
    lines.append("  ARCHIVOS GENERADOS")
    lines.append("-" * 60)
    for subdir in ("construcciones", "poblacion", "socioeconomica"):
        folder = paths[subdir]
        for f in sorted(os.listdir(folder)):
            lines.append(f"  {os.path.join(folder, f)}")
    lines.append("")
    lines.append("=" * 60)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  Log guardado: {log_path}")


# =========================================================
# IMPORTACIÓN DE CAPAS BASE GRASS (población y NBI)
# =========================================================

def import_base_layers_grass(roi_path, gid):
    """
    Importa al mapset de GRASS solo las capas que se procesan con GRASS:
    WorldPop (población) y NBI (socioeconómica).
    Las construcciones se procesan enteramente con rasterio/geopandas.

    Returns
    -------
    tuple[list[str], list[str], bool]
        (layers_imported, layers_reused, use_5m_dem)
    """
    imported = []
    reused   = []

    # --- Detectar DEM (solo para informar al log y process_construcciones) ---
    print("  Verificando cobertura del DEM 5m sobre el ROI...")
    use_5m_dem = dem_covers_roi(PATH_DEM_5m, roi_path)
    if use_5m_dem:
        print("  ✔ DEM 5m cubre el ROI: construcciones a 5m.")
    else:
        print("  ⚠ DEM 5m no cubre el ROI: construcciones a 30m.")

    # --- WorldPop 100m ---
    pop_map = f"population_density_gid_{gid}"

    if map_exists(pop_map, "raster"):
        print(f"  ✔ {pop_map} ya existe en el mapset, reutilizando.")
        reused.append(pop_map)
    else:
        print("  Importando WorldPop 100m...")
        gs.run_command("g.region", region="roi_region", res=100)
        gs.run_command(
            "r.import",
            input=PATH_WORLDPOP,
            output=pop_map,
            extent="region",
            overwrite=True
        )
        gs.run_command("r.null", map=pop_map, null="0")
        imported.append(pop_map)
        print(f"  ✔ {pop_map} importado.")

    # --- NBI ---
    if map_exists("NBI", "vector"):
        print("  ✔ NBI ya existe en el mapset, reutilizando.")
        reused.append("NBI")
    else:
        print("  Importando NBI...")
        gs.run_command("v.import", input=PATH_NBI, output="NBI", overwrite=True)
        imported.append("NBI")
        print("  ✔ NBI importado.")

    return imported, reused, use_5m_dem


# =========================================================
# VARIABLE: CONSTRUCCIONES (rasterio + numpy + geopandas)
# =========================================================

def process_construcciones(gid, paths, run_name, roi_path, use_5m_dem):
    """
    Calcula la variable de altura de construcciones enteramente con
    rasterio, numpy y geopandas: sin usar GRASS para evitar problemas
    de topología con polígonos solapados de Open Buildings.

    Flujo:
      1. Recortar DEM y FABDEM al ROI con rasterio.
      2. Si use_5m_dem: remuestrear FABDEM a 5m para que coincida con el DEM.
      3. Calcular Building_height = DEM - FABDEM, negativos → 0.
      4. Cargar polígonos de Open Buildings recortados al ROI (geopandas).
      5. Para cada polígono, extraer el máximo de Building_height con numpy.
      6. Rasterizar la altura máxima por polígono.
      7. Exportar la altura sin categorizar (metros) a resolución nativa.
      8. Categorizar y exportar a resolución nativa y a 100m.

    Categorización:
        altura == 0 o sin edificio → 0      (sin construcción)
        altura <= 3m               → 1      (muy baja)
        altura <= 9m               → 0.75   (baja)
        altura <= 18m              → 0.5    (media)
        altura >  18m              → 0.25   (alta)

    Parameters
    ----------
    gid : str
    paths : dict[str, str]
    run_name : str
    roi_path : str
    use_5m_dem : bool
    """
    print("\n--- Construcciones (rasterio) ---")

    dem_path    = PATH_DEM_5m if use_5m_dem else PATH_DEM_30m
    native_res  = 5 if use_5m_dem else 30

    # --- Leer ROI ---
    roi_gdf = gpd.read_file(roi_path)
    roi_shapes = [geom.__geo_interface__ for geom in roi_gdf.geometry]


    # --- Recortar DEM al ROI ---
    print(f"  Recortando DEM {native_res}m al ROI...")
    with rasterio.open(dem_path) as src:
        dem_crs = src.crs
        roi_gdf_reproj = roi_gdf.to_crs(dem_crs)
        roi_shapes_reproj = [g.__geo_interface__ for g in roi_gdf_reproj.geometry]
        dem_data, dem_transform = rio_mask(src, roi_shapes_reproj, crop=True)
        dem_meta = src.meta.copy()
        dem_meta.update({
            "height":    dem_data.shape[1],
            "width":     dem_data.shape[2],
            "transform": dem_transform,
            "nodata":    -9999
        })
        dem_nodata = src.nodata if src.nodata is not None else -9999
    dem_arr = dem_data[0].astype(np.float32)

    # --- Recortar FABDEM y reproyectar a la grilla exacta del DEM ---
    # Se reproyecta siempre a la grilla del DEM para garantizar
    # que ambos arrays tengan exactamente el mismo shape.
    print("  Recortando y alineando FABDEM a la grilla del DEM...")
    fab_aligned = np.full(dem_arr.shape, fill_value=-9999, dtype=np.float32)
    with rasterio.open(PATH_FABDEM) as src_fab:
        fab_nodata = src_fab.nodata if src_fab.nodata is not None else -9999
        reproject(
            source=rasterio.band(src_fab, 1),
            destination=fab_aligned,
            src_transform=src_fab.transform,
            src_crs=src_fab.crs,
            dst_transform=dem_transform,
            dst_crs=dem_crs,
            dst_nodata=-9999,
            resampling=Resampling.bilinear
        )
    fab_arr = fab_aligned

    
    # --- Altura de construcciones ---
    print("  Calculando altura de construcciones...")
    valid_mask = (dem_arr != dem_nodata) & (fab_arr != fab_nodata)
    height_arr = np.where(valid_mask, dem_arr - fab_arr, np.nan)
    height_arr = np.where(height_arr < 0, 0, height_arr)  # negativos → 0

    # --- Cargar Open Buildings recortado al ROI ---
    print("  Cargando Open Buildings para el ROI...")
    tiles_for_roi = get_tiles_for_roi(roi_path)
    buildings_list = []
    for tile_path in tiles_for_roi:
        gdf_tile = gpd.read_file(tile_path, mask=roi_gdf.to_crs(dem_crs))
        if not gdf_tile.empty:
            buildings_list.append(gdf_tile)

    if not buildings_list:
        raise RuntimeError(f"No se encontraron edificios en los tiles para gid={gid}")

    buildings = pd.concat(buildings_list, ignore_index=True)
    buildings = gpd.GeoDataFrame(buildings, crs=buildings_list[0].crs)
    buildings = buildings.to_crs(dem_crs)
    print(f"  Edificios cargados: {len(buildings)}")

    # --- Extraer altura máxima por polígono ---
    print("  Extrayendo altura máxima por polígono...")
    max_heights = []
    for geom in buildings.geometry:
        try:
            # Rasterizar este polígono como máscara temporal
            poly_mask = rio_rasterize(
                [(geom, 1)],
                out_shape=height_arr.shape,
                transform=dem_transform,
                fill=0,
                dtype=np.uint8
            )
            pixels = height_arr[poly_mask == 1]
            valid_pixels = pixels[~np.isnan(pixels)]
            max_h = float(np.max(valid_pixels)) if len(valid_pixels) > 0 else 0.0
        except Exception:
            max_h = 0.0
        max_heights.append(max_h)

    buildings["bh_maximum"] = max_heights
    print(f"  Altura máxima calculada: max={max(max_heights):.1f}m, "
          f"edificios con altura>0: {sum(h > 0 for h in max_heights)}")

    # --- Rasterizar altura máxima por polígono ---
    print("  Rasterizando altura máxima por polígono...")
    shapes_heights = [
        (geom, val)
        for geom, val in zip(buildings.geometry, buildings["bh_maximum"])
        if geom is not None and not geom.is_empty
    ]

    height_max_raster = rio_rasterize(
        shapes_heights,
        out_shape=height_arr.shape,
        transform=dem_transform,
        fill=999,       # sin edificio → 999
        dtype=np.float32
    )

    # --- Exportar altura sin categorizar (metros, sin edificio → 0) ---
    print("  Exportando altura sin categorizar...")
    raw_arr = np.where(height_max_raster == 999, 0, height_max_raster).astype(np.float64)

    raw_meta = dem_meta.copy()
    raw_meta.update({"dtype": "float64", "nodata": -9999, "count": 1})

    out_native_raw = os.path.join(
        paths["construcciones"],
        f"{run_name}_Buildings_raw_{native_res}m.tif"
    )
    with rasterio.open(out_native_raw, "w", **raw_meta) as dst:
        dst.write(raw_arr, 1)
    print(f"  ✔ Exportado: {out_native_raw}")

    # --- Categorización ---
    print("  Categorizando...")
    cat_arr = np.where(
        (height_max_raster == 0) | (height_max_raster == 999), 0,
        np.where(height_max_raster <= 3,  1,
        np.where(height_max_raster <= 9,  0.75,
        np.where(height_max_raster <= 18, 0.5,  0.25)))
    ).astype(np.float64)

    # --- Exportar a resolución nativa ---
    out_meta = dem_meta.copy()
    out_meta.update({"dtype": "float64", "nodata": -9999, "count": 1})

    out_native = os.path.join(
        paths["construcciones"],
        f"{run_name}_Buildings_cat_{native_res}m.tif"
    )
    with rasterio.open(out_native, "w", **out_meta) as dst:
        dst.write(cat_arr, 1)
    print(f"  ✔ Exportado: {out_native}")

    # --- Remuestrear a 100m (máximo) con rasterio ---
    print("  Remuestreando a 100m...")
    scale_factor = native_res / 100.0
    new_height = max(1, int(cat_arr.shape[0] * scale_factor))
    new_width  = max(1, int(cat_arr.shape[1] * scale_factor))

    from rasterio.transform import from_bounds
    from rasterio.enums import Resampling as RS

    bounds = rasterio.transform.array_bounds(
        cat_arr.shape[0], cat_arr.shape[1], dem_transform
    )
    transform_100m = from_bounds(*bounds, new_width, new_height)

    cat_100m = np.empty((new_height, new_width), dtype=np.float64)
    reproject(
        source=cat_arr,
        destination=cat_100m,
        src_transform=dem_transform,
        src_crs=dem_crs,
        dst_transform=transform_100m,
        dst_crs=dem_crs,
        resampling=RS.max
    )

    out_100m = os.path.join(
        paths["construcciones"], f"{run_name}_Buildings_cat_100m.tif"
    )
    meta_100m = out_meta.copy()
    meta_100m.update({
        "height": new_height,
        "width":  new_width,
        "transform": transform_100m
    })
    with rasterio.open(out_100m, "w", **meta_100m) as dst:
        dst.write(cat_100m, 1)
    print(f"  ✔ Exportado: {out_100m}")


# =========================================================
# VARIABLE: POBLACIÓN HUMANA (GRASS)
# =========================================================

def process_poblacion(paths, run_name, gid):
    """
    Categoriza WorldPop y exporta a 100m.

    Categorización:
        densidad <= 10  → 0
        densidad <= 20  → 0.25
        densidad <= 30  → 0.50
        densidad <= 40  → 0.75
        densidad >  40  → 1
    """
    pop_map = f"population_density_gid_{gid}"
    print("\n--- Población humana ---")
    gs.run_command("g.region", region="roi_region", res=100)

    out_100m_raw = os.path.join(paths["poblacion"], f"{run_name}_People_raw_100m.tif")
    gs.run_command(
        "r.out.gdal", input=pop_map, output=out_100m_raw,
        format="GTiff", type="Float64", nodata=-9999, flags="c", overwrite=True
    )
    print(f"  ✔ Exportado: {out_100m_raw}")

    gs.mapcalc(
            f"People_100 = "
            f"if({pop_map} <= 10, 0,"
            f"if({pop_map} <= 20, 0.25,"
            f"if({pop_map} <= 30, 0.50,"
            f"if({pop_map} <= 40, 0.75, 1))))",
            overwrite=True
        )
    out_100m = os.path.join(paths["poblacion"], f"{run_name}_People_100m.tif")
    gs.run_command(
        "r.out.gdal", input="People_100", output=out_100m,
        format="GTiff", type="Float64", nodata=-9999, flags="c", overwrite=True
    )
    print(f"  ✔ Exportado: {out_100m}")
    gs.run_command("g.region", region="roi_region", res=10)


# =========================================================
# VARIABLE: SOCIOECONÓMICA NBI (GRASS)
# =========================================================

def process_nbi(paths, run_name):
    """
    Rasteriza y categoriza NBI a 100m.

    Categorización:
        NBI <= 5%   → 0
        NBI <= 10%  → 0.25
        NBI <= 15%  → 0.50
        NBI <= 25%  → 0.75
        NBI >  25%  → 1
    """
    print("\n--- Socioeconómica (NBI) ---")
    gs.run_command("g.region", region="roi_region", res=100)
    gs.run_command(
        "v.to.rast", input="NBI", use="attr",
        attribute_column="H_NBI_prop_num", output="NBI_prop",
        type="area", memory=12000, overwrite=True
    )

    out_100m_raw = os.path.join(paths["socioeconomica"], f"{run_name}_NBI_raw_100m.tif")
    gs.run_command(
        "r.out.gdal", input="NBI_prop", output=out_100m_raw,
        format="GTiff", type="Float64", nodata=-9999, flags="c", overwrite=True
    )
    print(f"  ✔ Exportado: {out_100m_raw}")

    gs.mapcalc(
        "NBI_100 = "
        "if(NBI_prop <= 0.050, 0,"
        "if(NBI_prop <= 0.100, 0.25,"
        "if(NBI_prop <= 0.150, 0.50,"
        "if(NBI_prop <= 0.250, 0.75, 1))))",
        overwrite=True
    )
    out_100m = os.path.join(paths["socioeconomica"], f"{run_name}_NBI_100m.tif")
    gs.run_command(
        "r.out.gdal", input="NBI_100", output=out_100m,
        format="GTiff", type="Float64", nodata=-9999, flags="c", overwrite=True
    )
    print(f"  ✔ Exportado: {out_100m}")
    gs.run_command("g.region", region="roi_region", res=10)


# =========================================================
# MAIN
# =========================================================

def main():
    """
    Punto de entrada del pipeline de variables estáticas.

    Construcciones: procesado con rasterio/numpy/geopandas.
    Población y NBI: procesados con GRASS GIS.
    """
    print("\n=== VARIABLES ESTÁTICAS MCDA PIPELINE ===\n")
    print(f"  Directorio de salida: {OUTPUT_BASE}")
    print(f"  ROI cache:            {ROI_CACHE_DIR}\n")

    gid = input("GID de la localidad (ej: 1300 para Río Cuarto): ").strip()
    if not gid:
        raise ValueError("Debés ingresar un GID válido.")
    gid = sanitize_name(gid)

    roi_path = get_roi_path(gid)
    print(f"\n  ROI encontrado: {roi_path}")

    run_name = f"gid_{gid}_estaticas"
    run_base = os.path.join(OUTPUT_BASE, run_name)
    paths    = ensure_dirs(run_base)

    print("\nConfigurando región GRASS desde el ROI...")
    gs.run_command("v.import", input=roi_path, output="roi_work", overwrite=True)
    gs.run_command("g.region", vector="roi_work", res=10)
    gs.run_command("g.region", save="roi_region", overwrite=True)
    print("  Región guardada como 'roi_region'")

    print("\nVerificando capas base en el mapset (población y NBI)...")
    layers_imported, layers_reused, use_5m_dem = \
        import_base_layers_grass(roi_path, gid)

    try:
        process_construcciones(gid, paths, run_name, roi_path, use_5m_dem)
        process_poblacion(paths, run_name, gid)
        process_nbi(paths, run_name)
        success = True
    except Exception as e:
        print(f"\n  [ERROR] {e}")
        import traceback
        traceback.print_exc()
        success = False

    write_log(paths, gid, layers_imported, layers_reused, use_5m_dem, success)

    print("\n" + "=" * 55)
    print("  RESUMEN FINAL")
    print("=" * 55)
    if success:
        dem_usado = "5m (alta resolución)" if use_5m_dem else "30m (cobertura provincial)"
        print(f"\n  ✔ Variables estáticas generadas para gid={gid}")
        print(f"     DEM utilizado:  {dem_usado}")
        print(f"     Construcciones: {paths['construcciones']}")
        print(f"     Población:      {paths['poblacion']}")
        print(f"     NBI:            {paths['socioeconomica']}")
    else:
        print(f"\n  ✘ El procesamiento falló para gid={gid}")
        print("     Revisá el log para más detalles.")


if __name__ == "__main__":
    main()