#Para correr desde terminal: grass /home/tomas/grassdata/posgar2007_4_cba/MCDA --exec python3 /home/tomas/Desktop/Doctorado/espacializacion/vegetacion/calculo_vegetacion.py

# ROI Villa maria: /home/tomas/Desktop/Doctorado/espacializacion/vegetacion/_roi_cache/roi_gid_1252_1000m.gpkg 
"""
sentinel_ndvi_pipeline.py
=========================
Pipeline interactivo para:
  1. Definir un área de interés (ROI) desde un GPKG existente o desde un
     shapefile de municipios con buffer.
  2. Buscar y descargar TODAS las imágenes Sentinel-2 L2A de la ventana
     temporal indicada (vía EODAG / Copernicus Dataspace), sin filtrar
     por cobertura de nubes en la búsqueda.
  3. Para cada fecha disponible, ensamblar todas las teselas del día y
     evaluar la cobertura nubosa del mosaico sobre el ROI usando SCL (20 m):
       a. Si el mosaico supera el umbral de nubes → descartar el día.
       b. Si pasa → enmascarar píxeles nubosos en B04/B08 y calcular NDVI.
  4. Combinar NDVIs diarios en mediana del período (r.series ignora NoData).
  5. Remuestrear a 100 m y categorizar en 5 clases [0, 0.25, 0.5, 0.75, 1].
  6. Exportar resultados como GeoTIFF.
  7. Eliminar datos Sentinel crudos y generar log de procesamiento.

  Si todas las fechas de la semana son descartadas por nubes, retrocede
  automáticamente hasta MAX_WEEK_RETRIES semanas anteriores.

Parámetros internos configurables (sin necesidad de tocar otra lógica):
    OUTPUT_BASE              Directorio raíz de salida
    DAYS_BACK                Días de búsqueda hacia atrás (ventana temporal)
    MAX_PRODUCTS             Límite de escenas a descargar por semana
    MAX_WEEK_RETRIES         Máximo de semanas a reintentar hacia atrás
    SCL_CLOUD_CATEGORIES     Categorías SCL consideradas nube/sombra
    CLOUD_DISCARD_THRESHOLD  Fracción máxima de nubes del mosaico diario

Uso:
    grass <ruta/location/mapset> --exec python3 sentinel_ndvi_pipeline.py

Dependencias:
    grass-session / grass.script, eodag, geopandas

Credenciales Copernicus Dataspace (~/.config/eodag/eodag.yml):
    cop_dataspace:
        priority: 1
        auth:
            credentials:
                username: <email>
                password: "<contraseña>"
"""

import os
import re
import shutil
from datetime import datetime, timedelta
from collections import defaultdict

import geopandas as gpd
from eodag import EODataAccessGateway
import grass.script as gs


# =========================================================
# PARÁMETROS INTERNOS — editar acá para cambiar comportamiento
# =========================================================

# Directorio raíz donde se crean las carpetas de cada corrida
OUTPUT_BASE = "data/vegetacion"

# Días hacia atrás desde la fecha de referencia para definir la ventana
DAYS_BACK = 7

# Máximo de escenas a descargar por semana (None = sin límite)
# En una semana de 7 días sobre un ROI puntual raramente hay más de 10-15.
MAX_PRODUCTS = 20

# Máximo de semanas anteriores a reintentar si la semana pedida falla.
# En un sistema de alerta temprana no puede haber semanas sin dato por
# nubes -- se retrocede hasta encontrar la ultima semana con cobertura
# util. 52 es un techo de seguridad (un año), no un valor que se espere
# alcanzar en la práctica.
MAX_WEEK_RETRIES = 52

# Categorías SCL consideradas nube o sombra de nube:
#   3  = Cloud shadows
#   8  = Cloud medium probability
#   9  = Cloud high probability
#   10 = Thin cirrus
SCL_CLOUD_CATEGORIES = [3, 8, 9, 10]

# Fracción máxima de píxeles nubosos del MOSAICO DIARIO (sobre píxeles
# válidos del ROI) por encima de la cual se descarta el día completo.
# Se evalúa sobre el patch de todas las teselas del día, no por tesela.
CLOUD_DISCARD_THRESHOLD = 0.25

# fracción mínima del ROI que debe cubrir el mosaico diario
MIN_ROI_COVERAGE = 0.90  


# =========================================================
# UTILIDADES
# =========================================================

def ask_reference_date():
    """
    Solicita una fecha de referencia (extremo superior de la ventana de búsqueda).
    Si el usuario presiona Enter, usa la fecha de hoy.

    Returns
    -------
    datetime
    """
    raw = input(
        "Fecha de referencia para búsqueda [YYYY-MM-DD, Enter = hoy]: "
    ).strip()
    if raw == "":
        return datetime.today()
    return datetime.strptime(raw, "%Y-%m-%d")


def get_date_window(ref_date, days_back):
    """
    Calcula la ventana temporal [start_date, end_date].

    Parameters
    ----------
    ref_date : datetime
        Fecha de referencia (extremo superior de la ventana).
    days_back : int
        Días hacia atrás desde ref_date.

    Returns
    -------
    tuple[str, str]
        (start_date, end_date) en formato 'YYYY-MM-DD'.
    """
    start = ref_date - timedelta(days=days_back)
    return start.strftime("%Y-%m-%d"), ref_date.strftime("%Y-%m-%d")


def ensure_dirs(base_dir):
    """
    Crea la estructura de directorios del proyecto si no existe.

    Estructura generada:
        <base_dir>/
            data/
                sentinel_raw/   <- descargas originales (se borran al finalizar)
                roi/            <- archivos ROI generados
            outputs/
                ndvi/           <- NDVI_median y NDVI_100_max
                final/          <- NDVI categorizado final

    Parameters
    ----------
    base_dir : str
        Directorio raíz de la corrida (incluye run_name).

    Returns
    -------
    dict[str, str]
        Mapa de claves simbólicas a rutas absolutas.
    """
    paths = {
        "base":         base_dir,
        "data":         os.path.join(base_dir, "data"),
        "sentinel_raw": os.path.join(base_dir, "data", "sentinel_raw"),
        "roi":          os.path.join(base_dir, "data", "roi"),
        "outputs":      os.path.join(base_dir, "outputs"),
        "ndvi":         os.path.join(base_dir, "outputs", "ndvi"),
        "final":        os.path.join(base_dir, "outputs", "final"),
    }
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
    return paths


def sanitize_name(name):
    """
    Reemplaza caracteres no alfanuméricos (excepto '_') por '_',
    para usar la cadena como nombre de mapa válido en GRASS GIS.

    Parameters
    ----------
    name : str

    Returns
    -------
    str
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


# =========================================================
# LOG DE PROCESAMIENTO
# =========================================================

def write_processing_log(paths, run_name, start_date, end_date,
                         scene_info, daily_results, ndvi_maps, success,
                         weeks_back=0, requested_start=None, requested_end=None):
    """
    Genera un archivo de log .txt con el resumen del procesamiento
    de una semana: escenas descargadas, porcentaje de nubes del mosaico
    diario, qué días se descartaron/usaron, y archivos generados.

    El log se guarda en outputs/<run_name>_procesamiento.txt

    Parameters
    ----------
    paths : dict[str, str]
        Estructura de directorios de la corrida.
    run_name : str
        Nombre identificador de la corrida (usa la fecha SOLICITADA).
    start_date : str
        Fecha de inicio de la ventana de búsqueda REAL usada.
    end_date : str
        Fecha de fin de la ventana de búsqueda REAL usada.
    scene_info : list[dict]
        Lista de escenas detectadas (salida de detect_scenes).
    daily_results : list[dict]
        Resultados del filtrado por día:
        [{ date, cloud_pct, used, n_tiles }, ...]
    ndvi_maps : list[str]
        Mapas NDVI diarios calculados (vacío si la semana falló).
    success : bool
        True si la semana produjo resultado final.
    weeks_back : int
        Cuántas semanas se retrocedió respecto a la solicitada para
        conseguir cobertura útil. 0 = se usó la semana solicitada.
    requested_start, requested_end : str
        Ventana original solicitada (solo relevante si weeks_back > 0).
    """
    log_path = os.path.join(paths["outputs"], f"{run_name}_procesamiento.txt")

    lines = []
    lines.append("=" * 60)
    lines.append("  SENTINEL-2 NDVI PIPELINE — LOG DE PROCESAMIENTO")
    lines.append("=" * 60)
    lines.append(f"  Corrida:         {run_name}")
    lines.append(f"  Ventana buscada: {start_date} -> {end_date}")
    lines.append(f"  Fecha de log:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  Resultado:       {'EXITOSO' if success else 'FALLIDO (nubes)'}")
    lines.append("")

    if weeks_back > 0:
        lines.append("!" * 60)
        lines.append("  ATENCION -- DATO DE RESPALDO (semana solicitada sin cobertura util)")
        lines.append(f"  Semana solicitada:  {requested_start} -> {requested_end}")
        lines.append(f"  Semanas retrocedidas: {weeks_back}")
        lines.append(f"  Imagen realmente usada: {start_date} -> {end_date}")
        lines.append("!" * 60)
        lines.append("")

    lines.append("-" * 60)
    lines.append("  ESCENAS DETECTADAS EN DISCO")
    lines.append("-" * 60)
    if scene_info:
        for s in scene_info:
            lines.append(f"  {s['date']}  {s['scene_name']}")
    else:
        lines.append("  (ninguna)")
    lines.append("")

    lines.append("-" * 60)
    lines.append("  EVALUACIÓN SCL POR DÍA (mosaico de teselas)")
    lines.append(f"  Umbral de descarte: {CLOUD_DISCARD_THRESHOLD*100:.0f}%")
    lines.append(f"  Categorías enmascaradas: {SCL_CLOUD_CATEGORIES}")
    lines.append("-" * 60)
    if daily_results:
        days_used      = sum(1 for r in daily_results if r["used"])
        days_discarded = sum(1 for r in daily_results if not r["used"])
        for r in daily_results:
            estado = "USADO      " if r["used"] else "DESCARTADO"
            lines.append(
                f"  [{estado}]  {r['date']}  "
                f"nubes={r['cloud_pct']:5.1f}%  "
                f"cobertura={r.get('coverage_pct', 0):5.1f}%  "
                f"teselas={r['n_tiles']}"
            )
        lines.append("")
        lines.append(f"  Días usados:      {days_used}/{len(daily_results)}")
        lines.append(f"  Días descartados: {days_discarded}/{len(daily_results)}")
    else:
        lines.append("  (sin datos de evaluación SCL)")
    lines.append("")

    lines.append("-" * 60)
    lines.append("  NDVI DIARIOS CALCULADOS")
    lines.append("-" * 60)
    if ndvi_maps:
        for m in ndvi_maps:
            lines.append(f"  {m}")
    else:
        lines.append("  (ninguno — semana fallida)")
    lines.append("")

    lines.append("-" * 60)
    lines.append("  ARCHIVOS GENERADOS")
    lines.append("-" * 60)
    if success:
        for subdir in ("ndvi", "final"):
            folder = paths[subdir]
            for f in sorted(os.listdir(folder)):
                lines.append(f"  {os.path.join(folder, f)}")
    else:
        lines.append("  (ninguno — semana fallida)")
    lines.append("")

    lines.append("-" * 60)
    lines.append("  DATOS CRUDOS SENTINEL")
    lines.append("-" * 60)
    lines.append(
        "  Eliminados tras exportación exitosa."
        if success else
        "  Eliminados (semana fallida, sin resultado útil)."
    )
    lines.append("")
    lines.append("=" * 60)

    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"  Log guardado: {log_path}")


# =========================================================
# ROI
# =========================================================

def build_roi_from_municipio(municipios_path, gid_value, buffer_m, roi_out_path):
    """
    Construye un ROI en formato GPKG a partir de un shapefile de municipios,
    filtrando por el campo 'gid' y aplicando un buffer métrico.

    Pasos internos en GRASS:
      v.import -> v.extract (por gid) -> v.buffer -> v.out.ogr (GPKG)

    Parameters
    ----------
    municipios_path : str
        Ruta al shapefile (.shp) con todos los municipios.
    gid_value : int
        Valor del campo 'gid' del municipio deseado.
    buffer_m : int
        Buffer en metros alrededor del polígono del municipio.
    roi_out_path : str
        Ruta de salida del archivo GPKG resultante.

    Returns
    -------
    str
        Ruta al GPKG generado.
    """
    print("\nImportando municipios a GRASS...")
    gs.run_command(
        "v.import", input=municipios_path,
        output="municipios_arg_tmp", overwrite=True
    )

    print(f"Extrayendo municipio gid = {gid_value} ...")
    gs.run_command(
        "v.extract",
        input="municipios_arg_tmp",
        where=f"gid = {gid_value}",
        output="roi_municipio_tmp",
        overwrite=True
    )

    print(f"Generando buffer de {buffer_m} m ...")
    gs.run_command(
        "v.buffer",
        input="roi_municipio_tmp",
        distance=buffer_m,
        output="roi_buffer_tmp",
        overwrite=True
    )

    print(f"Exportando ROI a {roi_out_path} ...")
    if os.path.exists(roi_out_path):
        os.remove(roi_out_path)

    gs.run_command(
        "v.out.ogr",
        input="roi_buffer_tmp",
        output=roi_out_path,
        format="GPKG",
        overwrite=True
    )

    return roi_out_path


def get_roi_path(roi_cache_dir):
    """
    Solicita al usuario cómo definir el ROI y retorna la ruta al GPKG resultante.

    Opciones:
      1) Usar un GPKG de ROI ya existente en disco.
      2) Construirlo desde un shapefile de municipios, filtrando por gid
         y aplicando un buffer en metros.

    Parameters
    ----------
    roi_cache_dir : str                         
        Directorio donde se cachean los ROI generados desde shapefile.

    Returns
    -------
    str
        Ruta al GPKG del ROI.

    Raises
    ------
    FileNotFoundError
        Si el archivo indicado no existe en disco.
    ValueError
        Si la opción ingresada no es 1 ni 2, o si la ruta no termina en .shp.
    """
    print("\n--- ROI ---")
    print("  1) Usar un GPKG de ROI ya existente")
    print("  2) Construir ROI desde shapefile de municipios (filtrar por gid + buffer)")

    roi_mode = input("Elegí opción [1/2]: ").strip()

    if roi_mode == "1":
        default_roi = "/home/tomas/gisdata/GIS_MCDA/tile_cba_1km.gpkg"
        roi_path = input(
            f"Ruta al archivo GPKG del ROI [Enter = {default_roi}]: "
        ).strip()
        if roi_path == "":
            roi_path = default_roi
        if not os.path.exists(roi_path):
            raise FileNotFoundError(f"No se encontró el archivo: {roi_path}")
        return roi_path

    if roi_mode == "2":
        default_municipios = "/home/tomas/gisdata/GIS_MCDA/municipios_arg/municipio.shp"
        municipios_path = input(
            f"  Ruta al shapefile de municipios [Enter = {default_municipios}]: "
        ).strip()
        if municipios_path == "":
            municipios_path = default_municipios
        if not os.path.exists(municipios_path):
            raise FileNotFoundError(f"No se encontró el shapefile: {municipios_path}")
        if not municipios_path.lower().endswith(".shp"):
            raise ValueError(f"El archivo debe ser un .shp, se recibió: {municipios_path}")
    
        gid_value = int(input(
            "\nID del municipio (campo 'gid' en la tabla de atributos)\n"
            "  (ej: 1385 para Córdoba capital): "
        ).strip())
        buffer_m = input(
            "\nBuffer en metros alrededor del municipio [Enter = 1000]: "
        ).strip()
        buffer_m = int(buffer_m) if buffer_m else 1000

        roi_out_path = os.path.join(
            roi_cache_dir,  # ← carpeta permanente en lugar de paths["roi"]
            f"roi_gid_{gid_value}_{buffer_m}m.gpkg"
        )

        # Chequear si ya existe antes de reconstruir
        if os.path.exists(roi_out_path):
            print(f"\n  ROI ya existe en cache, reutilizando: {roi_out_path}")
            return roi_out_path

        return build_roi_from_municipio(
            municipios_path, gid_value, buffer_m, roi_out_path
        )

    raise ValueError("Opción inválida. Debe ser 1 o 2.")


# =========================================================
# BÚSQUEDA Y DESCARGA SENTINEL
# =========================================================

def search_and_download_sentinel(roi_path, sentinel_dir, start_date, end_date):
    """
    Busca y descarga imágenes Sentinel-2 L2A desde Copernicus Dataspace
    para el AOI y la ventana temporal indicados, sin filtrar por cobertura
    de nubes (el filtrado se hace localmente con la banda SCL).

    Se descargan hasta MAX_PRODUCTS escenas ordenadas por fecha.
    Las escenas ya presentes en sentinel_dir se saltean.
    Las credenciales se leen desde ~/.config/eodag/eodag.yml.

    Parameters
    ----------
    roi_path : str
        Ruta al GPKG del ROI (cualquier CRS; se reproyecta a EPSG:4326).
    sentinel_dir : str
        Directorio donde se guardarán las descargas.
    start_date : str
        Fecha de inicio en formato 'YYYY-MM-DD'.
    end_date : str
        Fecha de fin en formato 'YYYY-MM-DD'.

    Returns
    -------
    list[str]
        Rutas a los directorios/archivos descargados en esta ejecución.
    """
    print("\n--- Búsqueda Sentinel-2 ---")
    print(f"Ventana temporal: {start_date} -> {end_date}")

    gdf = gpd.read_file(roi_path).to_crs(4326)
    minx, miny, maxx, maxy = gdf.total_bounds

    dag = EODataAccessGateway()

    end_inclusive = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


    search_results = dag.search(
        collection="S2_MSI_L2A",
        start=start_date,
        end=end_inclusive,
        geom=[minx, miny, maxx, maxy]
    )

    all_products = sorted(
        search_results,
        key=lambda p: p.properties.get("start_datetime", "")
    )

    total_found = len(all_products)
    print(f"Escenas encontradas: {total_found}")

    if total_found == 0:
        print("No se encontraron escenas para el período y área indicados.")
        return []

    to_download = all_products[:MAX_PRODUCTS] if MAX_PRODUCTS else all_products

    print(f"\nEscenas a descargar: {len(to_download)} de {total_found}")
    print("Detalle:")
    for i, p in enumerate(to_download, 1):
        print(
            f"  {i:>3}.",
            p.properties.get("start_datetime", "?"),
            p.properties.get("title", p.properties.get("id", "?"))
        )

    print(f"\nDescargando {len(to_download)} escenas...")
    downloaded_paths = []
    existing = os.listdir(sentinel_dir)

    for p in to_download:
        scene_id = p.properties.get("title", p.properties.get("id", ""))

        if any(scene_id in d for d in existing):
            print(f"  ⏭  Ya existe, salteando: {scene_id}")
            continue

        try:
            path = dag.download(p, output_dir=sentinel_dir, extract=True)
            downloaded_paths.append(path)
            print(f"  ✔  {os.path.basename(path)}")
        except Exception as e:
            print(f"  ✘  Error descargando {scene_id}: {e}")

    print(f"\nDescargas completadas: {len(downloaded_paths)}/{len(to_download)}")
    return downloaded_paths


# =========================================================
# DETECCIÓN DE ESCENAS
# =========================================================

def detect_scenes(base_dir):
    """
    Recorre recursivamente base_dir buscando los archivos de bandas
    B04 (10 m), B08 (10 m) y SCL (20 m) dentro de la estructura
    estándar .SAFE de Sentinel-2.

    Solo se incluyen escenas que tengan los tres archivos presentes.
    Las escenas incompletas se reportan y se descartan.

    Parameters
    ----------
    base_dir : str
        Directorio raíz de las escenas descargadas.

    Returns
    -------
    list[dict]
        Lista ordenada por (fecha, nombre) de dicts con claves:
        scene_name (str), date (str YYYYMMDD),
        b04 (str), b08 (str), scl (str).
    """
    scene_dict = {}

    def get_safe_name(path):
        """Extrae el nombre base del directorio .SAFE desde una ruta completa."""
        for part in path.split(os.sep):
            if part.endswith(".SAFE"):
                return part.replace(".SAFE", "")
            if re.match(r"^S2[ABC]_MSIL2A_", part):
                return part
        return None

    for root, dirs, files in os.walk(base_dir):
        for f in files:
            full_path = os.path.join(root, f)
            safe_name = get_safe_name(full_path)
            if safe_name is None:
                continue

            scene_dict.setdefault(safe_name, {})

            if f.endswith("_B04_10m.tif") or f.endswith("_B04_10m.jp2"):
                scene_dict[safe_name]["b04"] = full_path
            elif f.endswith("_B08_10m.tif") or f.endswith("_B08_10m.jp2"):
                scene_dict[safe_name]["b08"] = full_path
            elif f.endswith("_SCL_20m.tif") or f.endswith("_SCL_20m.jp2"):
                scene_dict[safe_name]["scl"] = full_path

    scene_info = []
    for scene_name, bands in scene_dict.items():
        missing = [b for b in ("b04", "b08", "scl") if b not in bands]
        if missing:
            print(f"  [SKIP] Faltan bandas {missing}: {scene_name}")
            continue

        m = re.search(r"MSIL2A_(\d{8})T", scene_name)
        if not m:
            print(f"  [SKIP] No se pudo extraer fecha de: {scene_name}")
            continue

        scene_info.append({
            "scene_name": sanitize_name(scene_name),
            "date":       m.group(1),
            "b04":        bands["b04"],
            "b08":        bands["b08"],
            "scl":        bands["scl"],
        })

    return sorted(scene_info, key=lambda x: (x["date"], x["scene_name"]))


# =========================================================
# FILTRADO Y ENMASCARAMIENTO CON SCL — POR DÍA
# =========================================================

def compute_cloud_fraction(scl_map, cloud_categories):
    """
    Calcula la fracción de píxeles nubosos dentro de la región activa
    usando r.stats.

    Solo se cuentan píxeles con valor válido (se excluye NoData), por lo
    que el porcentaje es relativo a píxeles válidos del ROI.

    Parameters
    ----------
    scl_map : str
        Nombre del mapa SCL en GRASS.
    cloud_categories : list[int]
        Categorías SCL consideradas nube o sombra.

    Returns
    -------
    float
        Fracción de píxeles nubosos [0.0, 1.0]. Retorna 0.0 si no hay
        píxeles válidos.
    """
    stats_raw = gs.read_command(
        "r.stats", input=scl_map, flags="c", separator="space", quiet=True
    )

    total_valid = 0
    cloud_count = 0

    for line in stats_raw.strip().splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        val_str, count_str = parts
        if val_str == "*":
            continue
        try:
            val   = int(val_str)
            count = int(count_str)
        except ValueError:
            continue

        total_valid += count
        if val in cloud_categories:
            cloud_count += count

    if total_valid == 0:
        return 0.0

    return cloud_count / total_valid


def import_scl_for_scene(scene):
    """
    Importa la banda SCL de una escena a GRASS recortada al ROI a 20 m.

    La región debe estar configurada a roi_region/res=20 antes de llamar
    esta función.

    Parameters
    ----------
    scene : dict
        Dict con claves scene_name y scl.

    Returns
    -------
    str
        Nombre del mapa SCL importado en GRASS.
    """
    scl_map = f"{scene['scene_name']}_SCL"
    gs.run_command(
        "r.import", input=scene["scl"], output=scl_map,
        extent="region", resample="nearest", overwrite=True
    )
    gs.run_command("r.null", map=scl_map, setnull="0")
    return scl_map

def compute_roi_coverage(scl_map):

    gs.run_command("r.mask", vector="roi_work", overwrite=True)

    stats = gs.parse_command(
        "r.univar",
        map=scl_map,
        flags="g",
        quiet=True
    )

    gs.run_command("r.mask", flags="r")

    valid_cells = int(stats.get("n", 0))

    roi_stats = gs.parse_command(
        "r.univar",
        map="roi_mask",
        flags="g",
        quiet=True
    )

    roi_cells = int(roi_stats.get("n", 0))
    
    print("DEBUG")
    print("valid_cells =", valid_cells)
    print("roi_cells   =", roi_cells)
    print("coverage    =", valid_cells / roi_cells)
    return valid_cells / roi_cells if roi_cells > 0 else 0

def process_daily_scenes(scenes_for_date, cloud_categories, discard_threshold, min_coverage_threshold=MIN_ROI_COVERAGE):
    """
    Procesa todas las teselas de un mismo día como un conjunto:

      1. Importa las SCL de todas las teselas del día (a 20 m, recortadas al ROI).
      2. Ensambla las SCL en un mosaico diario con r.patch.
      3. Evalúa el % de nubes del MOSAICO completo sobre el ROI.
         — El umbral se aplica al ensamble del día, no a cada tesela
           individualmente. Así se evita que una tesela marginal con
           poca cobertura pase el filtro y genere franjas en el resultado.
      4. Si el mosaico supera el umbral → descartar el día completo.
      5. Si pasa → importar B04/B08 de todas las teselas, ensamblar,
         aplicar máscara de nubes del mosaico SCL.

    Parameters
    ----------
    scenes_for_date : list[dict]
        Escenas del mismo día (scene_name, date, b04, b08, scl).
    cloud_categories : list[int]
        Categorías SCL a enmascarar.
    discard_threshold : float
        Fracción máxima de nubes del mosaico diario.

    Returns
    -------
    tuple[dict or None, float, int]
        - dict con b04_daily y b08_daily (mapas enmascarados) si pasa,
          None si el día se descarta.
        - cloud_pct del mosaico diario (para el log).
        - coverage_pct de cobertura del ROI.
        - n_tiles: cantidad de teselas del día.
    """
    date      = scenes_for_date[0]["date"]
    n_tiles   = len(scenes_for_date)
    print(f"\n  Procesando fecha: {date}  ({n_tiles} tesela/s)")

    # --- 1. Importar SCL de todas las teselas a 20 m ---
    gs.run_command("g.region", region="roi_region", res=20)
    scl_maps = []
    for scene in scenes_for_date:
        scl_map = import_scl_for_scene(scene)
        scl_maps.append(scl_map)

    # --- 2. Ensamblar SCL en mosaico diario ---
    # r.patch usa el primer mapa como base y rellena sus NoData con los
    # siguientes. Para la SCL, el orden no importa porque las zonas sin
    # dato de una tesela son cubiertas por la tesela adyacente.
    scl_daily = f"SCL_daily_{date}"
    if len(scl_maps) > 1:
        gs.run_command(
            "r.patch", input=",".join(scl_maps),
            output=scl_daily, overwrite=True
        )
    else:
        gs.run_command(
            "g.copy", raster=f"{scl_maps[0]},{scl_daily}", overwrite=True
        )

    # --- 3. Evaluar cobertura y nubes sobre el mosaico completo del día ---
    
    # Primero: ¿el mosaico cubre suficiente área del ROI?
    print(gs.read_command("r.info", map=scl_daily, flags="g"))
    coverage_frac = compute_roi_coverage(scl_daily)
    coverage_pct  = coverage_frac * 100
    print(f"    Cobertura del ROI: {coverage_pct:.1f}%")

    if coverage_frac < min_coverage_threshold:
        print(f"    ✘ Día descartado (<{min_coverage_threshold*100:.0f}% de cobertura del ROI)")
        gs.run_command("g.region", region="roi_region", res=10)   
        maps_to_remove = ",".join(scl_maps + [scl_daily])
        gs.run_command(
            "g.remove", type="raster", name=maps_to_remove, flags="f", quiet=True
        )
        return None, 0.0, coverage_pct, n_tiles

    # Segundo: ¿cuántas nubes hay sobre lo que sí cubre?
    cloud_frac = compute_cloud_fraction(scl_daily, cloud_categories)
    cloud_pct  = cloud_frac * 100
    print(f"    Cobertura de nubes en mosaico diario: {cloud_pct:.1f}%")

    # Restaurar región a 10 m antes de cualquier decisión
    gs.run_command("g.region", region="roi_region", res=10)

    # --- 4. Descartar el día si supera el umbral ---
    if cloud_frac > discard_threshold:
        print(f"    ✘ Día descartado (>{discard_threshold*100:.0f}% nubes en mosaico)")
        # Limpiar mapas SCL temporales
        maps_to_remove = ",".join(scl_maps + [scl_daily])
        gs.run_command(
            "g.remove", type="raster", name=maps_to_remove, flags="f", quiet=True
        )
        return None, cloud_pct, coverage_pct, n_tiles

    # --- 5. Generar máscara de nubes del mosaico a 10 m ---
    # La región está a 10 m; GRASS resamplea scl_daily (20 m) con nearest
    # neighbor al vuelo, lo cual es correcto para datos categóricos.
    cloudmask_daily = f"cloudmask_daily_{date}"
    conditions = " || ".join(f"{scl_daily} == {c}" for c in cloud_categories)
    gs.mapcalc(
        f"{cloudmask_daily} = if({conditions}, 1, 0)",
        overwrite=True
    )

    # --- 6. Importar B04/B08 de todas las teselas y ensamblar a 10 m ---
    b04_tiles = []
    b08_tiles = []
    for scene in scenes_for_date:
        sn = scene["scene_name"]

        gs.run_command(
            "r.import", input=scene["b04"],
            output=f"{sn}_B04",
            extent="region", resample="nearest",
            resolution="region",
            overwrite=True
        )
        gs.run_command("r.null", map=f"{sn}_B04", setnull="0")
        gs.run_command(
            "r.import", input=scene["b08"],
            output=f"{sn}_B08",
            extent="region", resample="nearest",
            resolution="region",
            overwrite=True
        )
        gs.run_command("r.null", map=f"{sn}_B08", setnull="0")
        b04_tiles.append(f"{sn}_B04")
        b08_tiles.append(f"{sn}_B08")

    b04_daily = f"B04_daily_{date}"
    b08_daily = f"B08_daily_{date}"

    if len(b04_tiles) > 1:
        gs.run_command(
            "r.patch", input=",".join(b04_tiles),
            output=b04_daily, overwrite=True
        )
        gs.run_command(
            "r.patch", input=",".join(b08_tiles),
            output=b08_daily, overwrite=True
        )
    else:
        gs.run_command(
            "g.copy", raster=f"{b04_tiles[0]},{b04_daily}", overwrite=True
        )
        gs.run_command(
            "g.copy", raster=f"{b08_tiles[0]},{b08_daily}", overwrite=True
        )

    # --- 7. Aplicar máscara al mosaico diario ---
    # Donde cloudmask_daily == 1 (nube) → NoData; resto → valor original
    b04_masked = f"B04_masked_{date}"
    b08_masked = f"B08_masked_{date}"

    gs.mapcalc(
        f"{b04_masked} = if({cloudmask_daily} == 1, null(), {b04_daily})",
        overwrite=True
    )
    gs.mapcalc(
        f"{b08_masked} = if({cloudmask_daily} == 1, null(), {b08_daily})",
        overwrite=True
    )

    print(f"    ✔ Mosaico enmascarado — {cloud_pct:.1f}% píxeles de nubes")

    # --- Limpieza de mapas por tesela ---
    # Una vez patcheadas en el mosaico diario, las teselas individuales
    # (SCL/B04/B08 por escena) ya no hacen falta. Sin este borrado se
    # acumulan sin límite en el mapset (miles por año en produccion).
    per_tile_maps = ",".join(scl_maps + b04_tiles + b08_tiles)
    gs.run_command(
        "g.remove", type="raster", name=per_tile_maps, flags="f", quiet=True
    )

    return {
        "date": date,
        "b04":  b04_masked,
        "b08":  b08_masked,
    }, cloud_pct, coverage_pct, n_tiles


# =========================================================
# NDVI
# =========================================================

def process_all_dates(scene_info, cloud_categories, discard_threshold):
    """
    Agrupa las escenas por fecha y procesa cada día como un conjunto.

    Para cada fecha llama a process_daily_scenes(), que evalúa el
    mosaico completo del día antes de decidir si descartarlo.

    Parameters
    ----------
    scene_info : list[dict]
        Salida de detect_scenes().
    cloud_categories : list[int]
        Categorías SCL a enmascarar.
    discard_threshold : float
        Fracción máxima de nubes del mosaico diario para descarte.

    Returns
    -------
    tuple[list[dict], list[dict]]
        - Lista de dicts { date, b04, b08 } para los días válidos.
        - Lista de resultados diarios para el log:
          [{ date, cloud_pct, used, n_tiles }, ...]
    """
    # Agrupar escenas por fecha
    by_date = defaultdict(list)
    for scene in scene_info:
        by_date[scene["date"]].append(scene)

    valid_days   = []
    daily_results = []
    discarded    = 0

    for date in sorted(by_date.keys()):
        scenes = by_date[date]
        result, cloud_pct, coverage_pct, n_tiles = process_daily_scenes(
            scenes, cloud_categories, discard_threshold
        )

        day_used = result is not None
        daily_results.append({
            "date":      date,
            "cloud_pct": cloud_pct,
            "coverage_pct": coverage_pct,
            "used":      day_used,
            "n_tiles":   n_tiles,
        })

        if not day_used:
            discarded += 1
        else:
            valid_days.append(result)

    total = len(by_date)
    print(f"\nDías usados: {total - discarded}/{total}  ({discarded} descartados por nubes)")

    return valid_days, daily_results


def compute_daily_ndvi(valid_days):
    """
    Calcula el NDVI para cada día válido usando i.vi (viname=ndvi).

    Las bandas B04/B08 ya están ensambladas y enmascaradas (un mapa
    por día). Los píxeles NoData (nubes) se propagan al NDVI.

    NDVI = (NIR - RED) / (NIR + RED)

    Parameters
    ----------
    valid_days : list[dict]
        Salida de process_all_dates() — dicts con date, b04, b08.

    Returns
    -------
    list[str]
        Nombres de los mapas NDVI diarios creados en GRASS GIS.
    """
    ndvi_maps = []

    for day in valid_days:
        date          = day["date"]
        ndvi_date_map = f"NDVI_{date}"

        print(f"\n  Calculando NDVI: {date}")

        gs.run_command(
            "i.vi",
            red=day["b04"],
            nir=day["b08"],
            output=ndvi_date_map,
            viname="ndvi",
            overwrite=True
        )

        ndvi_maps.append(ndvi_date_map)
        print(f"  ✔ {ndvi_date_map}")

    return ndvi_maps


def compute_median(ndvi_maps):
    """
    Calcula la mediana píxel a píxel de todos los mapas NDVI diarios,
    generando el mapa 'NDVI_median' en GRASS GIS a 10 m.

    r.series con method=median ignora automáticamente los píxeles NoData,
    calculando la mediana solo con los valores válidos por posición.
    Si un píxel tiene nubes en todas las fechas, queda como NoData.

    Parameters
    ----------
    ndvi_maps : list[str]
        Nombres de los mapas NDVI diarios.

    Raises
    ------
    RuntimeError
        Si la lista de mapas está vacía.
    """
    if not ndvi_maps:
        raise RuntimeError("No se generaron mapas NDVI diarios.")

    # Asegurar que la región esté a 10 m para la mediana
    gs.run_command("g.region", region="roi_region", res=10)

    gs.run_command(
        "r.series",
        input=",".join(ndvi_maps),
        output="NDVI_median",
        method="median",
        overwrite=True
    )
    print("\nMapa creado: NDVI_median (10 m)")


def resample_and_categorize():
    """
    Remuestrea NDVI_median de 10 m a 100 m usando el valor máximo de cada
    celda de destino, y categoriza en 5 clases:

        NDVI <= 0.0  ->  0      (sin vegetación / suelo desnudo — no apto)
        NDVI >  0.6  ->  0.25   (vegetación muy densa — poco apto)
        NDVI <= 0.2  ->  0.5    (vegetación muy escasa — moderadamente apto)
        NDVI <= 0.4  ->  0.75   (vegetación escasa — muy apto)
        NDVI <= 0.6  ->  1      (vegetación moderada — óptimo)

    Cambia la región a 100 m solo para generar estos mapas derivados.
    La región se restaura a 10 m al terminar para no afectar exportaciones
    posteriores.

    Genera los mapas GRASS: NDVI_100_max, NDVI_cat.
    """
    # Cambiar resolución solo para los mapas a 100 m
    gs.run_command("g.region", res=100)

    gs.run_command(
        "r.resamp.stats",
        input="NDVI_median",
        output="NDVI_100_max",
        method="maximum",
        overwrite=True
    )

    gs.mapcalc(
        "NDVI_cat = if(NDVI_100_max <= 0.0, 0,"
        "          if(NDVI_100_max > 0.6,  0.25,"
        "          if(NDVI_100_max <= 0.2, 0.5,"
        "          if(NDVI_100_max <= 0.4, 0.75, 1))))",
        overwrite=True
    )

    # Restaurar región a 10 m para que export_outputs exporte NDVI_median
    # a su resolución nativa y no a 100 m
    gs.run_command("g.region", region="roi_region", res=10)

    print("Mapas creados: NDVI_100_max, NDVI_cat")


def export_outputs(paths, run_name):
    """
    Exporta los tres mapas de salida a GeoTIFF Float64.

        outputs/ndvi/<run_name>_NDVI_median_10m.tif    <- región a 10 m
        outputs/ndvi/<run_name>_NDVI_100_max_100m.tif  <- región a 100 m
        outputs/final/<run_name>_NDVI_cat_100m.tif     <- región a 100 m

    Cada mapa se exporta con la región ajustada a su resolución nativa
    para garantizar la resolución correcta en el GeoTIFF.

    flags="c" suprime la tabla de colores de GRASS (evita ERROR 6 de GDAL).
    nodata=-9999 marca explícitamente los píxeles sin dato en el GeoTIFF.

    Parameters
    ----------
    paths : dict[str, str]
        Estructura de directorios del proyecto.
    run_name : str
        Prefijo identificador de la corrida.
    """
    exports = [
        # (map_name, output_path, resolution)
        ("NDVI_median",  os.path.join(paths["ndvi"],  f"{run_name}_NDVI_median_10m.tif"),    10),
        ("NDVI_100_max", os.path.join(paths["ndvi"],  f"{run_name}_NDVI_100_max_100m.tif"), 100),
        ("NDVI_cat",     os.path.join(paths["final"], f"{run_name}_NDVI_cat_100m.tif"),      100),
    ]

    print("\nExportando resultados...")
    for map_name, out_path, res in exports:
        # Ajustar región a la resolución nativa del mapa antes de exportar
        gs.run_command("g.region", region="roi_region", res=res)
        gs.run_command(
            "r.out.gdal",
            input=map_name,
            output=out_path,
            format="GTiff",
            type="Float64",
            nodata=-9999,
            flags="c",
            overwrite=True
        )
        print(f"  ✔ {out_path}  ({res} m)")

    # Dejar la región en 10 m al salir
    gs.run_command("g.region", region="roi_region", res=10)


def cleanup_sentinel_raw(sentinel_dir):
    """
    Elimina el directorio de datos Sentinel crudos para liberar espacio.
    Los datos ya fueron procesados y exportados como GeoTIFF.

    Parameters
    ----------
    sentinel_dir : str
        Ruta al directorio sentinel_raw a eliminar.
    """
    if os.path.exists(sentinel_dir):
        print(f"\n  Eliminando datos crudos: {sentinel_dir}")
        shutil.rmtree(sentinel_dir)
        print("  ✔ Datos crudos eliminados.")
    else:
        print(f"\n  [INFO] Directorio ya no existe: {sentinel_dir}")


# =========================================================
# PROCESAMIENTO DE UNA SEMANA
# =========================================================

def run_week(localidad, start_date, end_date, roi_path,
             label_start=None, label_end=None, weeks_back=0):
    """
    Ejecuta el pipeline completo para una ventana temporal dada.

    Crea su propia estructura de directorios bajo OUTPUT_BASE,
    descarga escenas, procesa por mosaico diario (filtro SCL + enmascaramiento),
    calcula NDVI, exporta, genera log y elimina datos crudos.

    Parameters
    ----------
    localidad : str
        Nombre sanitizado de la localidad (prefijo de salida).
    start_date : str
        Fecha de inicio de la ventana de BÚSQUEDA real, formato 'YYYY-MM-DD'.
    end_date : str
        Fecha de fin de la ventana de búsqueda real, formato 'YYYY-MM-DD'.
    roi_path : str
        Ruta al GPKG del ROI.
    label_start, label_end : str, optional
        Ventana ORIGINALMENTE solicitada -- se usa para nombrar la carpeta
        y los archivos de salida, para que calculo_mcda.py siga
        encontrandolos por la fecha que en realidad le corresponde a la
        semana operativa, aunque la imagen sea de una semana anterior.
        Si no se pasan, se usan start_date/end_date (sin reintento).
    weeks_back : int
        Cuántas semanas se retrocedió respecto a lo solicitado (0 = la
        semana pedida tenía cobertura útil directamente). Solo afecta
        el cartel de aviso en el log.

    Returns
    -------
    bool
        True si se generaron salidas exitosamente, False si todas las
        fechas fueron descartadas por nubes o no se encontraron escenas.
    """
    label_start = label_start or start_date
    label_end   = label_end or end_date
    run_name = f"{localidad}_{label_start}_{label_end}_vegetacion"
    run_base = os.path.join(OUTPUT_BASE, run_name)
    paths    = ensure_dirs(run_base)

    print(f"\n{'='*55}")
    if weeks_back > 0:
        print(f"  Procesando semana SOLICITADA {label_start} -> {label_end}")
        print(f"  (retroceso de {weeks_back} semana/s -- buscando en {start_date} -> {end_date})")
    else:
        print(f"  Procesando semana: {start_date} -> {end_date}")
    print(f"  Carpeta: {run_base}")
    print(f"{'='*55}")

    # --- Búsqueda y descarga ---
    search_and_download_sentinel(
        roi_path=roi_path,
        sentinel_dir=paths["sentinel_raw"],
        start_date=start_date,
        end_date=end_date,
    )

    # --- Detección de escenas válidas (B04 + B08 + SCL) ---
    scene_info = detect_scenes(paths["sentinel_raw"])

    if not scene_info:
        print("  [SKIP] No se detectaron escenas válidas en disco.")
        write_processing_log(paths, run_name, start_date, end_date,
                             scene_info=[], daily_results=[],
                             ndvi_maps=[], success=False,
                             weeks_back=weeks_back, requested_start=label_start, requested_end=label_end)
        cleanup_sentinel_raw(paths["sentinel_raw"])
        return False

    print(f"\nEscenas detectadas: {len(scene_info)}")
    for s in scene_info:
        print(f"  {s['date']}  {s['scene_name']}")

    # --- Procesamiento por mosaico diario (filtro SCL + enmascaramiento) ---
    print(f"\nFiltro SCL — umbral de descarte: {CLOUD_DISCARD_THRESHOLD*100:.0f}%")
    print(f"Categorías enmascaradas: {SCL_CLOUD_CATEGORIES}")
    print("  (filtro aplicado sobre el mosaico diario completo, no por tesela)")

    valid_days, daily_results = process_all_dates(
        scene_info,
        cloud_categories=SCL_CLOUD_CATEGORIES,
        discard_threshold=CLOUD_DISCARD_THRESHOLD
    )

    if not valid_days:
        print("  [SKIP] Todos los días descartados por cobertura de nubes.")
        write_processing_log(paths, run_name, start_date, end_date,
                             scene_info=scene_info, daily_results=daily_results,
                             ndvi_maps=[], success=False,
                             weeks_back=weeks_back, requested_start=label_start, requested_end=label_end)
        cleanup_sentinel_raw(paths["sentinel_raw"])
        # Renombrar carpeta para distinguirla visualmente de corridas exitosas
        run_base_cloud = run_base + "_cloud"
        if os.path.exists(run_base) and not os.path.exists(run_base_cloud):
            os.rename(run_base, run_base_cloud)
        return False

    # --- NDVI diario ---
    ndvi_maps = compute_daily_ndvi(valid_days)

    if not ndvi_maps:
        print("  [SKIP] No se calcularon mapas NDVI.")
        write_processing_log(paths, run_name, start_date, end_date,
                             scene_info=scene_info, daily_results=daily_results,
                             ndvi_maps=[], success=False,
                             weeks_back=weeks_back, requested_start=label_start, requested_end=label_end)
        cleanup_sentinel_raw(paths["sentinel_raw"])
        return False

    print(f"\nNDVI diarios calculados: {ndvi_maps}")

    # --- Mediana, remuestreo, categorización y exportación ---
    compute_median(ndvi_maps)
    resample_and_categorize()
    export_outputs(paths, run_name)

    # Limpieza archivos innecesarios
    print("\n  Limpiando mapas intermedios del mapset GRASS...")
    intermediate_patterns = [
        "SCL_daily_*",
        "cloudmask_daily_*",
        "B04_daily_*",
        "B08_daily_*",
        "B04_masked_*",
        "B08_masked_*",
        "NDVI_2*",          # los NDVI diarios, ej. NDVI_20240312
    ]
    for pattern in intermediate_patterns:
        gs.run_command(
            "g.remove", type="raster",
            pattern=pattern,
            flags="f", quiet=True
        )
    # --- Log y limpieza ---
    write_processing_log(paths, run_name, start_date, end_date,
                         scene_info=scene_info, daily_results=daily_results,
                         ndvi_maps=ndvi_maps, success=True,
                         weeks_back=weeks_back, requested_start=label_start, requested_end=label_end)
    cleanup_sentinel_raw(paths["sentinel_raw"])

    return True


# =========================================================
# MAIN
# =========================================================

def main():
    """
    Punto de entrada del pipeline.

    Solo solicita dos datos al usuario:
      - Nombre corto de la localidad (prefijo de carpetas y archivos)
      - Fecha de referencia (extremo superior de la ventana de búsqueda)

    Todo lo demás está definido en los parámetros internos al inicio del archivo.

    Si la semana pedida falla por nubes, retrocede automáticamente hasta
    MAX_WEEK_RETRIES semanas anteriores. Al finalizar imprime un resumen
    de qué semanas fallaron y cuál produjo resultado.
    """
    print("\n=== SENTINEL-2 NDVI PIPELINE ===\n")
    print(f"  Directorio de salida: {OUTPUT_BASE}")
    print(f"  Ventana de búsqueda:  {DAYS_BACK} días")
    print(f"  Umbral de nubes:      {CLOUD_DISCARD_THRESHOLD*100:.0f}%  (por mosaico diario)")
    print(f"  Reintentos máximos:   {MAX_WEEK_RETRIES} semanas\n")

    # --- Identificador de la corrida ---
    localidad = input("Nombre corto de la localidad [Enter = cordoba]: ").strip().lower()
    if localidad == "":
        localidad = "cordoba"
    localidad = sanitize_name(localidad)

    # --- Fecha de referencia ---
    ref_date = ask_reference_date()
    start_date, end_date = get_date_window(ref_date, DAYS_BACK)
    print(f"\nVentana temporal inicial: {start_date} -> {end_date}")

    # --- ROI ---
    # La carpeta _roi_tmp se usa solo para que get_roi_path pueda escribir
    # el GPKG si se construye desde shapefile (opción 2). 
    roi_cache_dir = "resources/roi"
    os.makedirs(roi_cache_dir, exist_ok=True)
    roi_path    = get_roi_path(roi_cache_dir)

    # --- Región GRASS a 10 m sobre el ROI ---
    # Se guarda con nombre 'roi_region' una sola vez al inicio.
    # Todos los reintentos semanales reutilizan esta región guardada.
    print("\nConfigurando región GRASS a 10 m desde el ROI...")
    gs.run_command("v.import", input=roi_path, output="roi_work", overwrite=True)
    gs.run_command("g.region", vector="roi_work", res=20)
    gs.run_command("v.to.rast", input="roi_work", output="roi_mask", use="val", value=1, overwrite=True)
    gs.run_command("g.region", vector="roi_work", res=10)
    gs.run_command("g.region", save="roi_region", overwrite=True)
    print("  Región guardada como 'roi_region'")

    # --- Loop de reintentos semanales ---
    # label_start/label_end (la semana pedida) se mantienen fijos para
    # nombrar la salida -- lo que cambia entre intentos es la ventana de
    # BUSQUEDA (current_start/current_end), que retrocede hasta encontrar
    # cobertura util. calculo_mcda.py sigue encontrando el resultado por
    # la fecha operativa correcta, sin importar de qué semana vino la imagen.
    label_start, label_end = start_date, end_date
    failed_weeks  = []
    success_week  = None
    success_weeks_back = None
    current_start = datetime.strptime(start_date, "%Y-%m-%d")
    current_end   = datetime.strptime(end_date,   "%Y-%m-%d")

    for attempt in range(1, MAX_WEEK_RETRIES + 1):
        s = current_start.strftime("%Y-%m-%d")
        e = current_end.strftime("%Y-%m-%d")
        weeks_back = attempt - 1

        if attempt > 1:
            print(f"\n  Sin cobertura util en la semana solicitada -- "
                  f"retrocediendo a {s} -> {e}  "
                  f"(semana -{weeks_back}, intento {attempt}/{MAX_WEEK_RETRIES})")

        success = run_week(
            localidad=localidad,
            start_date=s,
            end_date=e,
            roi_path=roi_path,
            label_start=label_start,
            label_end=label_end,
            weeks_back=weeks_back,
        )

        if success:
            success_week = (s, e)
            success_weeks_back = weeks_back
            break
        else:
            failed_weeks.append((s, e))
            current_end   = current_start - timedelta(days=1)
            current_start = current_end   - timedelta(days=DAYS_BACK - 1)



    # --- Resumen final ---
    print("\n" + "=" * 55)
    print("  RESUMEN FINAL")
    print("=" * 55)

    if failed_weeks:
        print(f"\n  Semanas sin cobertura útil ({len(failed_weeks)}):")
        for s, e in failed_weeks:
            print(f"    ✘  {s} -> {e}")

    if success_week:
        s, e = success_week
        run_name  = f"{localidad}_{label_start}_{label_end}_vegetacion"
        final_dir = os.path.join(OUTPUT_BASE, run_name, "outputs", "final")
        log_path  = os.path.join(OUTPUT_BASE, run_name, "outputs",
                                 f"{run_name}_procesamiento.txt")
        print(f"\n  ✔ Resultado generado para la semana solicitada: {label_start} -> {label_end}")
        if success_weeks_back > 0:
            print(f"     ATENCIÓN: usó imagen de {success_weeks_back} semana/s atrás ({s} -> {e})")
        print(f"     GeoTIFF: {final_dir}")
        print(f"     Log:     {log_path}")
    else:
        print(f"\n  ✘ No se encontró ninguna semana con cobertura útil "
              f"en las últimas {MAX_WEEK_RETRIES} semanas.")
        print("     Intentá con una fecha de referencia más alejada en el tiempo.")


if __name__ == "__main__":
    main()