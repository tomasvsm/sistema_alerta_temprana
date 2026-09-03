"""
Dashboard del sistema de alerta temprana de Aedes aegypti (Córdoba, AR).

Muestra, por localidad y semana:
  - Índice de actividad espacial (idoneidad x oviposición), con la capa de
    error (sigma, desvío intra-semanal) como capa secundaria opcional.
  - Índice de oviposición (temporal), con el tramo proyectado a futuro
    (pronóstico CFS, ver modelo-temporal/src/actualizar_clima_semanal.py)
    diferenciado del dato confirmado.
  - Serie meteorológica cruda (precipitación, temperatura, humedad),
    colapsada por defecto.

Lee directo los archivos que producen los demás servicios (indice_actividad,
modelo-temporal) -- no tiene lógica de cálculo propia, solo lectura y
visualización.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import rasterio
import streamlit as st
import streamlit.components.v1 as components
from matplotlib import colors as mcolors
from rasterio.features import shapes as rio_shapes
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform as rio_transform
from streamlit_folium import st_folium
import folium
from folium import MacroElement
from jinja2 import Template

REPO_ROOT = Path(__file__).resolve().parent.parent
IA_DIR = REPO_ROOT / "espacializacion" / "output" / "indice_actividad"
MODELO_DIR = REPO_ROOT / "modelo-temporal" / "output"
ESTADO_JSON = REPO_ROOT / "orquestador" / "logs" / "estado_ultima_corrida.json"
ESTATICAS_DIR = REPO_ROOT / "espacializacion" / "estaticas"
VEGETACION_DIR = REPO_ROOT / "espacializacion" / "data" / "vegetacion"
MCDA_DIR = REPO_ROOT / "espacializacion" / "output" / "MCDA"

# Cordoba primero -- es la localidad default al abrir (selectbox sin index
# explicito toma la primera opcion del dict).
GID_NOMBRE = {
    "1385": "Córdoba",
    "1300": "Río Cuarto",
    "1252": "Villa María",
    "1271": "Salsipuedes",
}
GID_SNAKE = {
    "1385": "cordoba",
    "1300": "rio_cuarto",
    "1252": "villa_maria",
    "1271": "salsipuedes",
}
# Umbral de Youden propio de cada localidad: mejor punto de corte ROC
# (sensibilidad/especificidad) contra ovitrampas reales, de
# validacion/cruce/analisis_correlacion.ipynb (corrida 2026-08-05 11:48,
# sobre validacion/cruce/ovis_con_riesgo.csv). Villa Maria corregida a
# 0.1945 el 2026-09-02: el valor viejo (0.1544) quedo desactualizado tras
# un refresh del dataset de campo el 2026-08-05 y nunca se propago a
# produccion (bug real, confirmado comparando contra el notebook y sus
# backups previos al refresh).
YOUDEN = {"1252": 0.1945, "1271": 0.0829, "1300": 0.3736, "1385": 0.2360}

# Cortes de las categorias "media" y "alta": terciles del Rw real de las
# ovitrampas de esa localidad que superan su propio umbral de Youden
# (mismo dataset y mismo criterio de calibracion que YOUDEN, no una
# division geometrica del rango [Youden, 1.0] -- esa version anterior
# hacia que "muy alta" fuera virtualmente inalcanzable, porque el indice
# real nunca se acerca a 1.0). El piso de "baja" (0.0) y el techo tecnico
# de "muy alta" (1.0) no son terciles, son limites del rango valido.
TERCILES_CAMPO = {
    "1252": (0.296, 0.556),
    "1271": (0.264, 0.626),
    "1300": (0.410, 0.619),
    "1385": (0.359, 0.494),
}
PALETA = ["#2b83ba", "#83c1ab", "#e0f3b5", "#d7191c"]
CATEGORIAS = ["Actividad baja", "Actividad media", "Actividad alta", "Actividad muy alta"]

# Techo fijo de la escala de la capa de error (sigma): tiene que ser el
# mismo en todas las semanas para que los colores sean comparables entre
# si (si se recalculara por raster, la misma sigma real se veria distinta
# segun la semana). 0.15 cubre comodo el maximo real observado en los 244
# rasters de sigma existentes (0.129).
VMAX_SIGMA = 0.15


def bounds_categoricos(gid: str) -> list[float]:
    q33, q66 = TERCILES_CAMPO[gid]
    return [0.0, YOUDEN[gid], q33, q66, 1.0]


def codigo_categoria_maxima(gid: str, arr: np.ndarray) -> int:
    """Categoria MAS ALTA presente entre los pixeles validos de esta
    semana (-1 si no hay datos), no la mas frecuente: un solo pixel en una
    categoria superior ya sube el semaforo a ese nivel. Criterio de
    alerta temprana -- mas sensible que el promedio/moda, que casi
    siempre daria "baja" porque la mayoria del area esta genuinamente
    baja la mayor parte del tiempo (ver hallazgo del 2026-09-01)."""
    validos = arr[~np.isnan(arr)]
    if validos.size == 0:
        return -1
    codigos = np.digitize(validos, bounds_categoricos(gid)[1:-1])
    return int(codigos.max())


def _texto_legible_sobre(color_hex: str) -> str:
    """Negro o blanco segun la luminancia del color de fondo -- algunos
    colores de PALETA (ej. "alta", verde amarillento muy palido) son
    ilegibles con texto blanco encima."""
    r, g, b = (int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
    luminancia = 0.299 * r + 0.587 * g + 0.114 * b
    return "#1a1a1a" if luminancia > 150 else "#ffffff"


def _hex_con_alpha(color_hex: str, alpha: float) -> str:
    r, g, b = (int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{alpha})"


def fmt_fecha(iso: str) -> str:
    """YYYY-MM-DD -> DD/MM/YYYY. Todas las fechas que se muestran como
    texto (selectores, titulos, sliders) usan este mismo formato -- antes
    convivian con el formato ISO de los nombres de archivo, lo que
    quedaba inconsistente entre distintas partes del dashboard."""
    return f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}"


class ControlRecentrar(MacroElement):
    """Boton "volver a la vista inicial" para el mapa: Leaflet no trae uno
    propio, y despues de hacer zoom/paneo manual no hay forma de recentrar
    sin recargar toda la pagina. Va como MacroElement (igual que
    LayerControl) y no como HTML/JS insertado a mano, porque
    streamlit-folium solo ejecuta los <script> que folium arma por su
    mecanismo normal de render(); cualquier otra cosa insertada a mano en
    root.html o root.script queda muerta (probado, ver historial)."""

    _template = Template("""
        {% macro script(this, kwargs) %}
        (function() {
            var mapa = {{ this._parent.get_name() }};
            var limites = L.latLngBounds({{ this.bounds }});
            var ControlBtn = L.Control.extend({
                options: {position: 'topleft'},
                onAdd: function() {
                    var btn = L.DomUtil.create('button', 'leaflet-bar leaflet-control');
                    btn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" ' +
                        'fill="none" stroke="black" stroke-width="2" stroke-linecap="round" ' +
                        'stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/>' +
                        '</svg>';
                    btn.title = 'Volver a la vista inicial';
                    btn.style.cssText = 'width:30px;height:30px;cursor:pointer;' +
                        'background:white;display:flex;align-items:center;' +
                        'justify-content:center;';
                    L.DomEvent.on(btn, 'click', function(e) {
                        L.DomEvent.stopPropagation(e);
                        mapa.fitBounds(limites);
                    });
                    return btn;
                },
            });
            mapa.addControl(new ControlBtn());

            // Cuando .block-container se achica para @media print, el
            // iframe del mapa tambien se achica (eso si pasa solo, es CSS
            // normal) -- pero Leaflet no se entera solo: sin invalidateSize
            // sigue posicionando los tiles con las cuentas del tamaño
            // viejo, y el mapa queda mostrado a medias / corrido. Este
            // window es el de adentro del iframe del propio mapa (no el
            // de la app), pero beforeprint/matchMedia print igual llegan
            // aca porque el iframe forma parte del mismo trabajo de
            // impresion que la pagina que lo contiene.
            window.matchMedia('print').addEventListener('change', function() {
                mapa.invalidateSize();
            });
            window.addEventListener('beforeprint', function() { mapa.invalidateSize(); });
        })();
        {% endmacro %}
    """)

    def __init__(self, bounds):
        super().__init__()
        self._name = "ControlRecentrar"
        self.bounds = bounds


class LeyendaError(MacroElement):
    """Referencia de la capa de error (sigma): un recuadro con la barra de
    color, oculto por defecto y que solo se muestra mientras esa capa este
    prendida (enganchado a overlayadd/overlayremove del propio control de
    capas de Leaflet) -- si no, ocupa lugar en el mapa todo el tiempo aunque
    la capa este apagada la mayoria de las veces."""

    _template = Template("""
        {% macro script(this, kwargs) %}
        (function() {
            var mapa = {{ this._parent.get_name() }};
            var caja = L.DomUtil.create('div', 'leaflet-bar');
            caja.style.cssText = 'display:none; background:white; padding:6px 10px; ' +
                'font-size:11px; line-height:1.3; border-radius:4px;';
            caja.innerHTML = '<div style="font-weight:600; margin-bottom:3px;">Error (σ)</div>' +
                '<div style="width:110px; height:10px; border-radius:2px; border:1px solid #ccc; ' +
                'background:linear-gradient(to right, #ffffff, #d4b9da, #c994c7, #df65b0, #67001f);"></div>' +
                '<div style="display:flex; justify-content:space-between; margin-top:2px;">' +
                '<span>0</span><span>{{ "%.2f"|format(this.vmax) }}</span></div>';
            var LeyendaControl = L.Control.extend({
                options: {position: 'bottomright'},
                onAdd: function() { return caja; },
            });
            mapa.addControl(new LeyendaControl());
            mapa.on('overlayadd', function(e) {
                if (e.name === {{ this.nombre_capa | tojson }}) { caja.style.display = 'block'; }
            });
            mapa.on('overlayremove', function(e) {
                if (e.name === {{ this.nombre_capa | tojson }}) { caja.style.display = 'none'; }
            });
        })();
        {% endmacro %}
    """)

    def __init__(self, vmax: float, nombre_capa: str):
        super().__init__()
        self._name = "LeyendaError"
        self.vmax = vmax
        self.nombre_capa = nombre_capa


def footer_html() -> str:
    return (
        '<div style="text-align:center; opacity:0.75; font-size:0.85rem; '
        'display:flex; flex-direction:column; align-items:center; gap:4px;">'
        '<a href="https://github.com/tomasvsm/sistema_alerta_temprana" '
        'target="_blank" style="text-decoration:none; color:inherit; '
        'display:inline-flex; align-items:center; gap:6px;">'
        '<svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor">'
        '<path fill-rule="evenodd" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 '
        '5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-'
        '2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 '
        '1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-'
        '.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 '
        '0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-'
        '1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 '
        '3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 '
        '2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z">'
        "</path></svg>"
        "github.com/tomasvsm/sistema_alerta_temprana</a>"
        "<span>Tomás V. San Miguel · Hecho con Streamlit</span>"
        "</div>"
    )


def semaforo_html(codigo_activo: int) -> str:
    """Recuadro propio con titulo y las 4 categorias siempre visibles (la
    silueta completa, con relleno translucido del color propio para que se
    entienda que forman un conjunto), resaltando solo la que corresponde a
    la semana seleccionada -- como un semaforo real, no un solo pill de
    texto. Grilla 2x2 (no una fila de 4) para poder vivir angosto, en el
    espacio libre junto a los selectores de localidad/semana."""
    pills = []
    etiquetas = [c.replace("Actividad ", "") for c in CATEGORIAS]
    for i, (color, etiqueta) in enumerate(zip(PALETA, etiquetas)):
        if i == codigo_activo:
            texto = _texto_legible_sobre(color)
            estilo = (
                f"background:{color}; color:{texto}; font-weight:700; "
                f"box-shadow:0 1px 4px rgba(0,0,0,0.35);"
            )
        else:
            # Texto siempre en un gris neutro (no en el color de la
            # categoria): un color palido como texto sobre fondo blanco
            # es tan ilegible como texto blanco sobre ese mismo color.
            estilo = (
                f"background:{_hex_con_alpha(color, 0.035)}; color:rgba(107,107,107,0.55); "
                f"font-weight:500; border:1.5px solid {_hex_con_alpha(color, 0.18)};"
            )
        pills.append(
            f'<div style="{estilo} text-align:center; padding:6px 4px; '
            f'border-radius:8px; font-size:0.82rem;">{etiqueta}</div>'
        )
    filas = "".join(pills)
    return (
        '<div style="border:1px solid rgba(128,128,128,0.35); border-radius:10px; '
        'padding:8px 12px 10px 12px; margin:0 0 6px 0;">'
        '<div style="font-size:0.72rem; text-transform:uppercase; letter-spacing:0.04em; '
        'opacity:0.65; margin-bottom:6px; display:flex; align-items:center; gap:5px;">'
        '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" '
        'style="flex-shrink:0;">'
        '<rect x="7" y="1" width="10" height="22" rx="4" stroke="currentColor" '
        'stroke-width="1.6"/>'
        '<circle cx="12" cy="6.5" r="2" fill="#d7191c"/>'
        '<circle cx="12" cy="12" r="2" fill="#e0c23a"/>'
        '<circle cx="12" cy="17.5" r="2" fill="#2b9e4a"/>'
        '</svg>'
        "Nivel de actividad de esta semana</div>"
        '<div style="display:grid; grid-template-columns:1fr 1fr; gap:6px;">'
        f"{filas}</div>"
        "</div>"
    )


@st.cache_data
def semanas_disponibles(gid: str) -> list[str]:
    patron = re.compile(rf"^(\d{{4}}-\d{{2}}-\d{{2}})_{gid}_indice_actividad\.tif$")
    fechas = [m.group(1) for f in IA_DIR.iterdir() if (m := patron.match(f.name))]
    return sorted(fechas, reverse=True)


@st.cache_data
def cargar_raster_4326(path: str):
    """Reproyecta a EPSG:4326 y devuelve (array, bounds) listos para folium."""
    with rasterio.open(path) as src:
        with WarpedVRT(src, crs="EPSG:4326", resampling=rasterio.enums.Resampling.nearest) as vrt:
            arr = vrt.read(1)
            bounds = vrt.bounds
            nodata = vrt.nodata
    arr = np.where(arr == nodata, np.nan, arr)
    return arr, [[bounds.bottom, bounds.left], [bounds.top, bounds.right]]


@st.cache_data
def contorno_roi_4326(path: str) -> list[list[list[float]]]:
    """Anillos (exterior + agujeros, ej. nubes enmascaradas) del limite del
    ejido/ROI, trazados directo desde la mascara de nodata del propio
    raster y reproyectados a EPSG:4326 -- para dibujar un borde prolijo
    en vez de dejar los bordes de pixel crudos contra el mapa base
    (mismo criterio que draw_roi_outline en el generador de PDFs)."""
    with rasterio.open(path) as src:
        arr = src.read(1)
        nodata = src.nodata if src.nodata is not None else -9999
        mask = (arr != nodata).astype(np.uint8)
        anillos = []
        for geom, valor in rio_shapes(mask, mask=None, transform=src.transform):
            if valor != 1:
                continue
            for anillo in [geom["coordinates"][0], *geom["coordinates"][1:]]:
                xs = [c[0] for c in anillo]
                ys = [c[1] for c in anillo]
                lons, lats = rio_transform(src.crs, "EPSG:4326", xs, ys)
                anillos.append([[lat, lon] for lat, lon in zip(lats, lons)])
    return anillos


def raster_a_imagen_rgba(arr: np.ndarray, gid: str | None, cmap_continuo: bool) -> np.ndarray:
    if cmap_continuo:
        norm = mcolors.Normalize(vmin=0, vmax=VMAX_SIGMA)
        cmap = mcolors.LinearSegmentedColormap.from_list(
            "sigma", ["#ffffff", "#d4b9da", "#c994c7", "#df65b0", "#67001f"]
        )
    else:
        norm = mcolors.BoundaryNorm(bounds_categoricos(gid), len(PALETA))
        cmap = mcolors.ListedColormap(PALETA)

    rgba = cmap(norm(np.nan_to_num(arr, nan=0.0)))
    rgba[np.isnan(arr), 3] = 0.0
    return (rgba * 255).astype(np.uint8)


@st.cache_data
def cargar_raster_nativo(path: str) -> np.ndarray:
    """Lee el raster en su CRS original (5346), sin reproyectar -- para
    graficos estaticos (matplotlib) que no van sobre un mapa base."""
    with rasterio.open(path) as src:
        arr = src.read(1).astype(np.float32)
        nodata = src.nodata
    return np.where(arr == nodata, np.nan, arr)


@st.cache_data
def serie_temporal_indice_actividad(gid: str) -> pd.DataFrame:
    """Promedio y maximo espacial del indice de actividad, por semana."""
    filas = []
    for fecha in semanas_disponibles(gid):
        arr = cargar_raster_nativo(str(IA_DIR / f"{fecha}_{gid}_indice_actividad.tif"))
        if np.all(np.isnan(arr)):
            continue
        filas.append({"date": fecha, "media": np.nanmean(arr), "maximo": np.nanmax(arr)})
    df = pd.DataFrame(filas)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date")


def _colorscale_escalonada(colores: list[str]) -> list:
    """Colorscale de Plotly con bandas solidas (sin degrade) para un mapa
    categorico -- cada color ocupa 1/n del rango, sin interpolar con el
    siguiente."""
    n = len(colores)
    escala = []
    for i, c in enumerate(colores):
        escala.append([i / n, c])
        escala.append([(i + 1) / n, c])
    return escala


@st.cache_data
def stack_categorias_indice_actividad(gid: str) -> tuple[np.ndarray, list[str]]:
    """Todas las semanas disponibles como un stack (semana, alto, ancho) de
    codigos de categoria (0-3), en orden cronologico -- para el visor
    animado."""
    fechas = list(reversed(semanas_disponibles(gid)))
    cortes = bounds_categoricos(gid)
    capas = []
    for fecha in fechas:
        arr = cargar_raster_nativo(str(IA_DIR / f"{fecha}_{gid}_indice_actividad.tif"))
        codigo = np.digitize(arr, cortes[1:-1]).astype(float)
        codigo[np.isnan(arr)] = np.nan
        capas.append(codigo)
    return np.stack(capas), fechas


def figura_animada_indice_actividad(gid: str) -> go.Figure:
    stack, fechas = stack_categorias_indice_actividad(gid)
    fig = px.imshow(
        stack, animation_frame=0,
        color_continuous_scale=_colorscale_escalonada(PALETA),
        range_color=[0, len(PALETA)],
        aspect="equal",
    )
    fig.update_traces(hoverinfo="skip", hovertemplate=None)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(
        coloraxis_showscale=False, height=420,
        margin=dict(t=10, b=10, l=10, r=10),
    )
    for i, frame in enumerate(fig.frames):
        frame.name = fechas[i]
    slider = fig.layout.sliders[0]
    nuevos_steps = []
    for i, step in enumerate(slider.steps):
        step_dict = step.to_plotly_json()
        step_dict["label"] = fmt_fecha(fechas[i])
        step_dict["args"] = [[fechas[i]], step_dict["args"][1]]
        nuevos_steps.append(step_dict)
    slider.steps = nuevos_steps
    slider.currentvalue = dict(prefix="Semana: ")

    # Arranca mostrando la semana mas reciente (ultimo frame), no la mas
    # vieja -- animation_frame de Plotly siempre pone el frame 0 primero
    # por defecto.
    ultimo = len(fig.frames) - 1
    fig.data[0].z = fig.frames[ultimo].data[0].z
    slider.active = ultimo

    return fig


# Mismos 5 colores (Viridis discreto en 0/0.25/0.5/0.75/1) y mismas
# etiquetas de categoria que las figuras del manuscrito
# (manuscrito/figuras/variables*.png) -- estas 4 variables ya vienen
# categorizadas en esos 5 valores exactos desde el procesamiento (ver
# calculo_mcda.py y calculo_vegetacion.py), no son continuas.
PALETA_VIRIDIS5 = ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"]

# Rutas fijas de las 3 variables estaticas del MCDA (no cambian semana a
# semana, una sola por localidad -- ver espacializacion/src/calculo_mcda.py).
VARIABLES_ESTATICAS = {
    "construcciones": (
        "construcciones", "Buildings_cat_100m", "Altura de construcciones",
        ["Sin construcciones", "Edificios altos", "Edificios medios", "Edificios bajos", "Casas"],
    ),
    "poblacion": (
        "poblacion", "People_100m", "Población",
        ["0 a 10", "10 a 20", "20 a 30", "30 a 40", "> 40"],
    ),
    "nbi": (
        "socioeconomica", "NBI_100m", "NBI",
        ["< 5%", "5% a 10%", "10% a 15%", "15% a 25%", "> 25%"],
    ),
}
CATEGORIAS_NDVI = ["Sin vegetación", "Muy densa", "Muy escasa", "Escasa", "Moderada"]


@st.cache_data
def cargar_variable_estatica(gid: str, variable: str) -> np.ndarray | None:
    subdir, sufijo, _, _ = VARIABLES_ESTATICAS[variable]
    ruta = ESTATICAS_DIR / f"gid_{gid}_estaticas" / subdir / f"gid_{gid}_estaticas_{sufijo}.tif"
    if not ruta.exists():
        return None
    return cargar_raster_nativo(str(ruta))


def leyenda_categorica_html(etiquetas: list[str]) -> str:
    return " · ".join(
        f'<span style="color:{c}">■</span> {e}' for c, e in zip(PALETA_VIRIDIS5, etiquetas)
    )


def figura_categorica_5(arr: np.ndarray) -> go.Figure:
    """Las 4 capas ya vienen con exactamente estos 5 valores (0, 0.25,
    0.5, 0.75, 1) -- alcanza con escalarlas a indice de color 0-4, no hace
    falta digitize."""
    codigo = np.round(arr * 4)
    fig = px.imshow(
        codigo, color_continuous_scale=_colorscale_escalonada(PALETA_VIRIDIS5),
        range_color=[0, 5], aspect="equal",
    )
    fig.update_traces(hoverinfo="skip", hovertemplate=None)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(
        height=300, coloraxis_showscale=False, margin=dict(t=10, b=10, l=10, r=10),
    )
    return fig


@st.cache_data
def vegetacion_disponible(gid: str) -> dict[str, str]:
    """fecha de fin -> ruta al NDVI categorico de esa semana."""
    nombre = GID_SNAKE[gid]
    patron = re.compile(rf"^{nombre}_(\d{{4}}-\d{{2}}-\d{{2}})_(\d{{4}}-\d{{2}}-\d{{2}})_vegetacion$")
    resultado = {}
    if not VEGETACION_DIR.is_dir():
        return resultado
    for d in VEGETACION_DIR.iterdir():
        if not d.is_dir():
            continue
        m = patron.match(d.name)
        if not m:
            continue
        fecha_fin = m.group(2)
        tif = d / "outputs" / "final" / f"{d.name}_NDVI_cat_100m.tif"
        if tif.exists():
            resultado[fecha_fin] = str(tif)
    return resultado


@st.cache_data
def stack_vegetacion(gid: str) -> tuple[np.ndarray, list[str]]:
    disponibles = vegetacion_disponible(gid)
    fechas = sorted(disponibles.keys())
    capas = [cargar_raster_nativo(disponibles[f]) for f in fechas]
    return np.stack(capas), fechas


def figura_animada_vegetacion(gid: str) -> go.Figure:
    stack, fechas = stack_vegetacion(gid)
    codigos = np.round(stack * 4)
    fig = px.imshow(
        codigos, animation_frame=0,
        color_continuous_scale=_colorscale_escalonada(PALETA_VIRIDIS5),
        range_color=[0, 5], aspect="equal",
    )
    fig.update_traces(hoverinfo="skip", hovertemplate=None)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(
        height=420, margin=dict(t=10, b=10, l=10, r=10), coloraxis_showscale=False,
    )
    for i, frame in enumerate(fig.frames):
        frame.name = fechas[i]
    slider = fig.layout.sliders[0]
    nuevos_steps = []
    for i, step in enumerate(slider.steps):
        step_dict = step.to_plotly_json()
        step_dict["label"] = fmt_fecha(fechas[i])
        step_dict["args"] = [[fechas[i]], step_dict["args"][1]]
        nuevos_steps.append(step_dict)
    slider.steps = nuevos_steps
    slider.currentvalue = dict(prefix="Semana: ")

    ultimo = len(fig.frames) - 1
    fig.data[0].z = fig.frames[ultimo].data[0].z
    slider.active = ultimo

    return fig


@st.cache_data
def cargar_idoneidad(gid: str, semana: str) -> np.ndarray | None:
    """Salida cruda del MCDA (idoneidad de habitat, 0-1) para una semana
    puntual -- la misma capa que despues se multiplica por la oviposicion
    diaria para dar el indice de actividad."""
    ruta = MCDA_DIR / f"{semana}_{gid}_MCDA.tif"
    if not ruta.exists():
        return None
    return cargar_raster_nativo(str(ruta))


def figura_idoneidad(gid: str, arr: np.ndarray) -> go.Figure:
    """Mismas 4 categorias/colores que el mapa de indice de actividad
    (bounds_categoricos), para que se lean igual -- ver semaforo_html."""
    cortes = bounds_categoricos(gid)
    codigo = np.digitize(arr, cortes[1:-1]).astype(float)
    codigo[np.isnan(arr)] = np.nan
    fig = px.imshow(
        codigo, color_continuous_scale=_colorscale_escalonada(PALETA),
        range_color=[0, len(PALETA)], aspect="equal",
    )
    fig.update_traces(hoverinfo="skip", hovertemplate=None)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(
        height=420, coloraxis_showscale=False, margin=dict(t=10, b=10, l=10, r=10),
    )
    return fig


@st.cache_data
def cargar_indice_oviposicion(gid: str) -> pd.DataFrame | None:
    nombre = GID_SNAKE[gid]
    candidatos = sorted(MODELO_DIR.glob(f"*_{gid}_{nombre}_indice_oviposicion.csv"))
    if not candidatos:
        return None
    df = pd.read_csv(candidatos[-1], parse_dates=["date"])
    return df


@st.cache_data
def cargar_serie_meteorologica(gid: str) -> pd.DataFrame | None:
    nombre = GID_SNAKE[gid]
    candidatos = sorted(MODELO_DIR.glob(f"*_{gid}_{nombre}_modelo.csv"))
    if not candidatos:
        return None
    df = pd.read_csv(candidatos[-1], parse_dates=["date"])
    return df[["date", "precipitations", "temperature", "rh"]]


# Formato de fecha de los ejes X: numerico puro (nada de "Jan"/"Aug" en
# ingles) y adaptativo segun el zoom -- dia/mes cuando se distinguen
# semanas individuales, mes/año o solo año cuando la serie abarca varios
# años (un tickformat fijo tipo "%Y-%m" repite la misma etiqueta para
# varias semanas del mismo mes y no deja ver de que semana se trata).
TICKFORMATSTOPS_FECHA = [
    dict(dtickrange=[None, "M1"], value="%d/%m/%y"),
    dict(dtickrange=["M1", "M12"], value="%m/%Y"),
    dict(dtickrange=["M12", None], value="%Y"),
]


def fig_indice_oviposicion(df_ovip: pd.DataFrame, titulo: str, dias_atras: int | None, height: int) -> go.Figure:
    """dias_atras=None -> serie completa disponible, sin recortar."""
    hoy = pd.Timestamp(date.today())
    real = df_ovip[df_ovip["date"] <= hoy]
    pronost = df_ovip[df_ovip["date"] >= hoy]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=real["date"], y=real["indice_oviposicion"],
        mode="lines", name="Histórico", line=dict(color="#1f77b4"),
    ))
    fin_pronost = hoy
    if not pronost.empty:
        fin_pronost = pronost["date"].max()
        fig.add_vrect(x0=hoy, x1=fin_pronost, fillcolor="#1f77b4", opacity=0.08, line_width=0)
        fig.add_trace(go.Scatter(
            x=pronost["date"], y=pronost["indice_oviposicion"],
            mode="lines", name="Pronosticado (14 días)",
            line=dict(color="#e07b39", width=2.5),
        ))
    fig.add_vline(x=hoy, line_dash="dot", line_color="gray")
    inicio = (hoy - pd.Timedelta(days=dias_atras)) if dias_atras else df_ovip["date"].min()
    fig.update_layout(
        title=titulo, xaxis_title=None, height=height,
        margin=dict(t=50, b=10, l=10, r=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0.5, xanchor="center"),
        xaxis_range=[inicio, fin_pronost + pd.Timedelta(days=3)],
        xaxis=dict(tickformatstops=TICKFORMATSTOPS_FECHA),
        # rango hasta 1.1 para que los valores cercanos a 1 no queden
        # pegados/cortados contra el borde superior; el tick sigue en 1.
        yaxis=dict(title="Índice (0-1)", range=[0, 1.1], dtick=0.5),
    )
    return fig


def cargar_estado_orquestador() -> dict | None:
    if not ESTADO_JSON.exists():
        return None
    with open(ESTADO_JSON) as f:
        return json.load(f)


# --------------------------------------------------------------------------
st.set_page_config(page_title="Alerta temprana Aedes aegypti", layout="wide")
st.markdown(
    """<style>
    header[data-testid="stHeader"] { background: transparent; height: 2.5rem; }
    div[data-testid="stAppDeployButton"] { display: none; }
    .block-container { padding-top: 0.8rem; }
    div[data-testid="stHeading"]:has(h1) { text-align: center; }
    div[data-testid="stHeading"] h1 { font-size: 2rem; }
    div[data-testid="stSlider"] { margin: -10px 0 -8px 0; }
    div[data-testid="stLayoutWrapper"]:has(.st-key-mapa_centrado) { align-self: center; }

    /* Reporte imprimible: la app no esta pensada para pantallas angostas,
       asi que sin esto el navegador imprime el layout ancho de pantalla
       tal cual, cortado en el borde de la hoja. */
    @media print {
        div[data-testid="stAppViewContainer"] { overflow: visible !important; }
        div[data-testid="stToolbarActions"],
        div[data-testid="stTabs"] [data-baseweb="tab-list"],
        div[data-testid="stSlider"],
        div[data-testid="stFullScreenFrame"] button,
        .leaflet-control-zoom { display: none !important; }
        /* Esto NO alcanza solo, queda por si ayuda en algo, pero el
           bloqueo real es el atributo HTML inert (no display/CSS) que
           Streamlit pone en el contenido de un expander cerrado --
           Chromium excluye lo inert de la impresion sin importar el CSS.
           El arreglo de verdad (sacar inert + abrir el <details>) esta en
           el componente JS de mas abajo, enganchado al cambio de la media
           query "print". */
        details:not([open]) > *:not(summary) { display: block !important; }
        [data-testid="stExpanderDetails"] { display: block !important; height: auto !important; }
        div[data-testid="stElementContainer"],
        div[data-testid="stHorizontalBlock"],
        div[data-testid="stCustomComponentV1"],
        div[data-testid="stPlotlyChart"],
        div[data-testid="stExpander"] {
            break-inside: avoid;
            /* Chrome respeta page-break-inside mas consistente que
               break-inside en su motor de impresion -- sin esto el primer
               grafico de oviposicion se partia justo en el borde de hoja
               (probado con el dialogo de impresion real, no solo con
               page.pdf() de Playwright). */
            page-break-inside: avoid;
        }
        /* No max-width:100% -- eso hereda el ancho de LA VENTANA del
           usuario (que puede ser mucho mas ancha que la hoja), y como el
           mapa es responsive (width:100% de su columna) pero los graficos
           de Plotly no (ancho fijo en pixeles, calculado en pantalla),
           un contenedor mas ancho que la hoja hace que el mapa "se
           estire" de mas mientras los graficos quedan chicos y fijos --
           eso es lo que se ve como que el mapa crece solo. Un ancho fijo
           en px, mas cercano al ancho real de una hoja A4 apaisada
           (~281mm de zona imprimible a 96dpi), evita ese desajuste. */
        .block-container { max-width: 1050px !important; padding: 0.3rem 0.5rem !important; }
        /* El recorte real: los graficos de Plotly quedan dibujados como
           SVG con el ancho fijo en pixeles que tenian en pantalla (nunca
           en el ancho de la hoja), y no hay ningun evento de impresion
           que Plotly escuche para redibujarse mas angosto -- ni siquiera
           JS inyectado a mano llega a tiempo (probado: Chromium recalcula
           el layout al tamaño de hoja recien en el paso final de
           impresion, sin re-ejecutar JS). Un CSS "zoom" a nivel pagina lo
           arregla pero rompe el mapa (Leaflet no recalcula su tamaño dentro
           de un iframe zoomeado, se corta por un borde -- probado). No hay
           arreglo limpio solo por CSS: la opcion real es bajar la escala
           en el dialogo de impresion del navegador antes de imprimir
           (Mas opciones > Escala > ~60%), que si funciona bien porque
           reescala la pagina ya renderizada entera, iframes incluidos. */
        @page { size: A4 landscape; margin: 8mm; }
    }
    </style>""",
    unsafe_allow_html=True,
)

# El CSS de arriba no alcanza para que el contenido de un expander cerrado
# salga en el PDF: Streamlit marca ese contenido con el atributo HTML
# inert, y Chromium excluye lo inert de la impresion sin importar el
# display/CSS (probado). Sacar el atributo si requiere JS -- por eso va en
# un componente (iframe same-origin, con acceso a window.parent), enganchado
# al cambio de la media query "print" en vez de a beforeprint/afterprint
# (mas confiable entre motores de impresion, incluida la de Playwright).
# Se marca cada expander que se abrio asi para volver a cerrarlo despues,
# sin tocar los que el usuario ya tenia abiertos por su cuenta.
components.html(
    """<script>
    function prepararImpresion(activar) {
        var detalles = window.parent.document.querySelectorAll('[data-testid="stExpanderDetails"]');
        detalles.forEach(function(det) {
            var details = det.closest('details');
            if (!details) return;
            if (activar) {
                if (det.hasAttribute('inert') && !details.hasAttribute('open')) {
                    det.setAttribute('data-reabierto-para-imprimir', '1');
                    det.removeAttribute('inert');
                    details.setAttribute('open', '');
                }
            } else if (det.hasAttribute('data-reabierto-para-imprimir')) {
                det.removeAttribute('data-reabierto-para-imprimir');
                det.setAttribute('inert', '');
                details.removeAttribute('open');
            }
        });
        if (!activar) return;
        // El CSS de @media print SI achica .block-container (confirmado),
        // pero Plotly dibuja el SVG de cada grafico con el ancho en pixeles
        // que tenia la columna en pantalla y no hay redibujado automatico
        // -- forzarlo a mano. beforeprint (ademas de matchMedia) porque
        // segun el motor de impresion uno de los dos puede no llegar a
        // tiempo antes de que se capture la pagina (probado: con
        // Page.printToPDF de Chromium/Playwright, matchMedia solo a veces
        // no alcanzaba).
        var graficos = window.parent.document.querySelectorAll('.js-plotly-plot');
        graficos.forEach(function(gd) {
            if (window.parent.Plotly) { window.parent.Plotly.Plots.resize(gd); }
        });
    }
    window.parent.matchMedia('print').addEventListener('change', function(e) {
        prepararImpresion(e.matches);
    });
    window.parent.addEventListener('beforeprint', function() { prepararImpresion(true); });
    window.parent.addEventListener('afterprint', function() { prepararImpresion(false); });
    </script>""",
    height=0,
)

estado = cargar_estado_orquestador()
if estado and estado.get("hubo_error"):
    pasos_error = [p for p, s in estado["pasos"].items() if str(s).startswith("ERROR")]
    st.error(
        f"⚠️ La última corrida semanal ({estado['fecha_ref']}) tuvo errores en: "
        f"**{', '.join(pasos_error)}**. Los datos mostrados pueden no estar actualizados "
        f"en esas etapas. Log: `{estado['log']}`"
    )

st.title("Sistema de alerta temprana de actividad de *Aedes aegypti*")

tab_panel, tab_acerca = st.tabs(["Panel", "Acerca de"])

with tab_panel:
    col_sel_loc, col_sel_sem, col_semaforo = st.columns([2, 2, 5])
    with col_sel_loc:
        gid = st.selectbox(
            "Localidad", options=list(GID_NOMBRE), format_func=lambda g: GID_NOMBRE[g],
            filter_mode=None,
        )
    semanas = semanas_disponibles(gid)
    if not semanas:
        st.warning("No hay semanas procesadas para esta localidad todavía.")
        st.stop()

    # Semana: selectbox arriba + barra de tiempo junto al mapa, ambos
    # controlan el mismo valor (session_state) para poder elegir una fecha
    # exacta o simplemente arrastrar. Se guarda por localidad para no
    # perder la semana elegida al ir y volver entre localidades.
    ESTADO_SEMANA_KEY = f"semana_actual_{gid}"
    if st.session_state.get(ESTADO_SEMANA_KEY) not in semanas:
        st.session_state[ESTADO_SEMANA_KEY] = semanas[0]

    def _semana_desde_selectbox():
        valor = st.session_state[f"{ESTADO_SEMANA_KEY}_select"]
        st.session_state[ESTADO_SEMANA_KEY] = valor
        st.session_state[f"{ESTADO_SEMANA_KEY}_slider"] = valor

    def _semana_desde_slider():
        valor = st.session_state[f"{ESTADO_SEMANA_KEY}_slider"]
        st.session_state[ESTADO_SEMANA_KEY] = valor
        st.session_state[f"{ESTADO_SEMANA_KEY}_select"] = valor

    with col_sel_sem:
        st.selectbox(
            "Semana (fecha de fin)", options=semanas, format_func=fmt_fecha,
            index=semanas.index(st.session_state[ESTADO_SEMANA_KEY]),
            key=f"{ESTADO_SEMANA_KEY}_select", on_change=_semana_desde_selectbox,
            filter_mode=None,
        )
    semana = st.session_state[ESTADO_SEMANA_KEY]

    ia_path = IA_DIR / f"{semana}_{gid}_indice_actividad.tif"
    sigma_path = IA_DIR / f"{semana}_{gid}_sigma.tif"
    arr_ia, bounds = cargar_raster_4326(str(ia_path))
    codigo_activo = codigo_categoria_maxima(gid, arr_ia)

    with col_semaforo:
        st.markdown(semaforo_html(codigo_activo), unsafe_allow_html=True)

    col_mapa, col_ovip = st.columns([3, 2])

    with col_mapa:
        # Titulo, barra de tiempo y mapa comparten el mismo ancho fijo del
        # mapa (610px) y quedan centrados dentro de la columna -- si no,
        # el titulo y la barra (que si son responsive) quedaban mas anchos
        # que el mapa (fijo) y todo se veia desalineado. st.container con
        # width= es un elemento real (a diferencia de un <div> suelto en
        # st.markdown, que Streamlit renderiza aislado y no envuelve a los
        # hermanos siguientes).
        with st.container(width=610, key="mapa_centrado"):
            referencias = " · ".join(
                f'<span style="color:{c}">■</span> {cat.replace("Actividad ", "")}'
                for c, cat in zip(PALETA, CATEGORIAS)
            )
            st.markdown(
                f"**Índice de actividad** ({referencias})", unsafe_allow_html=True
            )

            semanas_cronologico = list(reversed(semanas))
            st.select_slider(
                "Recorrer semanas", options=semanas_cronologico,
                value=semana, key=f"{ESTADO_SEMANA_KEY}_slider",
                on_change=_semana_desde_slider, label_visibility="collapsed",
                format_func=fmt_fecha,
            )

            centro = [(bounds[0][0] + bounds[1][0]) / 2, (bounds[0][1] + bounds[1][1]) / 2]

            m = folium.Map(location=centro, tiles=None)
            folium.TileLayer(
                tiles="https://server.arcgisonline.com/ArcGIS/rest/services/"
                      "Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
                attr="Esri",
                name="Claro", show=True,
            ).add_to(m)
            folium.TileLayer(
                tiles="https://server.arcgisonline.com/ArcGIS/rest/services/"
                      "World_Imagery/MapServer/tile/{z}/{y}/{x}",
                attr="Esri",
                name="Satelital", show=False,
            ).add_to(m)
            folium.raster_layers.ImageOverlay(
                image=raster_a_imagen_rgba(arr_ia, gid, cmap_continuo=False),
                bounds=bounds,
                name="Índice de actividad",
                opacity=0.75,
            ).add_to(m)

            for anillo in contorno_roi_4326(str(ia_path)):
                folium.PolyLine(
                    locations=anillo, color="#8a8a8a", weight=1.2, opacity=0.8,
                ).add_to(m)

            if sigma_path.exists():
                arr_sigma, bounds_sigma = cargar_raster_4326(str(sigma_path))
                nombre_capa_sigma = "Error (σ, desvío intra-semanal)"
                capa_sigma = folium.raster_layers.ImageOverlay(
                    image=raster_a_imagen_rgba(arr_sigma, None, cmap_continuo=True),
                    bounds=bounds_sigma,
                    name=nombre_capa_sigma,
                    opacity=0.75,
                    show=False,
                )
                capa_sigma.add_to(m)
                LeyendaError(VMAX_SIGMA, nombre_capa_sigma).add_to(m)

            folium.LayerControl(collapsed=True).add_to(m)
            m.fit_bounds(bounds)

            ControlRecentrar(bounds).add_to(m)

            st_folium(m, height=460, width=610, returned_objects=[])

        st.caption(
            f"Umbral de Youden de esta localidad: {YOUDEN[gid]:.4f} "
            "(calibrado contra datos de ovitrampas)."
        )

    with col_ovip:
        df_ovip = cargar_indice_oviposicion(gid)
        if df_ovip is None:
            st.info("Sin datos de índice de oviposición para esta localidad.")
        else:
            st.plotly_chart(
                fig_indice_oviposicion(
                    df_ovip, "Índice de oviposición: últimos 3 meses + pronóstico",
                    dias_atras=90, height=270,
                ),
                width=410,
            )
            desde = df_ovip["date"].min().strftime("%Y-%m-%d")
            st.plotly_chart(
                fig_indice_oviposicion(
                    df_ovip, f"Índice de oviposición: desde {fmt_fecha(desde)}",
                    dias_atras=None, height=270,
                ),
                width=410,
            )

    with st.expander("Evolución del índice de actividad (serie temporal y mapas animados)"):
        df_serie = serie_temporal_indice_actividad(gid)
        if df_serie.empty:
            st.info("Sin semanas suficientes para mostrar evolución.")
        else:
            fig_serie = go.Figure()
            fig_serie.add_trace(go.Scatter(
                x=df_serie["date"], y=df_serie["media"],
                mode="lines+markers", name="Media espacial", line=dict(color="#d7191c"),
            ))
            fig_serie.add_trace(go.Scatter(
                x=df_serie["date"], y=df_serie["maximo"],
                mode="lines", name="Máximo espacial",
                line=dict(color="#d7191c", dash="dot", width=1),
            ))
            fig_serie.update_layout(
                height=420, margin=dict(t=30, b=10, l=10, r=10),
                yaxis_title="Índice de actividad", xaxis_title=None,
                legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0.5, xanchor="center"),
                xaxis=dict(tickformatstops=TICKFORMATSTOPS_FECHA),
            )

            col_evol_serie, col_evol_mapa = st.columns(2)
            with col_evol_serie:
                st.plotly_chart(fig_serie, width=465)
            with col_evol_mapa:
                st.caption(
                    "Arrastrá el control para ver cualquier semana disponible, o tocá "
                    "Play para recorrerlas todas."
                )
                st.plotly_chart(figura_animada_indice_actividad(gid), width=465)

    with st.expander("Variables espaciales (MCDA)"):
        referencias_idoneidad = " · ".join(
            f'<span style="color:{c}">■</span> {cat.replace("Actividad ", "")}'
            for c, cat in zip(PALETA, CATEGORIAS)
        )
        st.markdown(
            f"**Índice de idoneidad de hábitat** ({referencias_idoneidad}) "
            f"&mdash; semana finalizada el {fmt_fecha(semana)}",
            unsafe_allow_html=True,
        )
        arr_idoneidad = cargar_idoneidad(gid, semana)
        if arr_idoneidad is None:
            st.info("Sin datos de idoneidad para esta semana.")
        else:
            st.plotly_chart(figura_idoneidad(gid, arr_idoneidad), width=950)

        st.divider()

        col_v1, col_v2, col_v3 = st.columns(3)
        for col, variable in zip((col_v1, col_v2, col_v3), ("construcciones", "poblacion", "nbi")):
            with col:
                arr_var = cargar_variable_estatica(gid, variable)
                _, _, titulo_var, etiquetas_var = VARIABLES_ESTATICAS[variable]
                if arr_var is None:
                    st.info(f"Sin datos de {titulo_var.lower()} para esta localidad.")
                else:
                    st.markdown(f"**{titulo_var}**")
                    st.plotly_chart(figura_categorica_5(arr_var), width=310)
                    st.markdown(
                        "<br>".join(
                            f'<span style="color:{c}">■</span> {e}'
                            for c, e in zip(PALETA_VIRIDIS5, etiquetas_var)
                        ),
                        unsafe_allow_html=True,
                    )

        st.divider()

        st.markdown(
            f"**Vegetación (NDVI)** ({leyenda_categorica_html(CATEGORIAS_NDVI)})",
            unsafe_allow_html=True,
        )
        if not vegetacion_disponible(gid):
            st.info("Sin datos de vegetación para esta localidad.")
        else:
            st.plotly_chart(figura_animada_vegetacion(gid), width=950)

    with st.expander("Datos meteorológicos"):
        df_met = cargar_serie_meteorologica(gid)
        if df_met is None:
            st.info("Sin datos meteorológicos para esta localidad.")
        else:
            hoy = pd.Timestamp(date.today())
            ventana = df_met

            fig_met = go.Figure()
            fig_met.add_trace(go.Bar(
                x=ventana["date"], y=ventana["precipitations"],
                name="Precipitación (mm)", marker_color="#4a90d9", yaxis="y1",
            ))
            fig_met.add_trace(go.Scatter(
                x=ventana["date"], y=ventana["temperature"],
                name="Temperatura (°C)", line=dict(color="#e07b39"), yaxis="y2",
            ))
            fig_met.add_trace(go.Scatter(
                x=ventana["date"], y=ventana["rh"],
                name="Humedad relativa (%)", line=dict(color="#5aa469"), yaxis="y2",
            ))
            fig_met.add_vline(x=hoy, line_dash="dot", line_color="gray")
            fig_met.update_layout(
                height=350, margin=dict(t=50, b=10, l=10, r=10),
                yaxis=dict(title="Precipitación (mm)"),
                yaxis2=dict(title="°C / %", overlaying="y", side="right"),
                legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0.5, xanchor="center"),
                # Datos completos desde siempre en el grafico (autoscale
                # los muestra), pero al abrir arranca con zoom al ultimo
                # año -- el rango largo completo casi no se distingue.
                xaxis=dict(
                    tickformatstops=TICKFORMATSTOPS_FECHA,
                    range=[hoy - pd.Timedelta(days=365), hoy],
                ),
            )
            st.plotly_chart(fig_met, width=960)

    st.divider()
    st.markdown(footer_html(), unsafe_allow_html=True)

with tab_acerca:
    st.header("Acerca de este sistema")
    st.markdown(
        """
Este sistema estima, semana a semana y por zona, la actividad de
*Aedes aegypti* (el mosquito vector del dengue) en cuatro localidades de
Córdoba: Córdoba capital, Río Cuarto, Villa María y Salsipuedes.

### Cómo se calcula el índice de actividad

El índice combina dos modelos que se calculan por separado y después se
multiplican entre sí.

**1. Modelo temporal (dinámica poblacional).** Es una implementación del
modelo de Aguirre
([Aguirre et al., 2021](https://doi.org/10.1016/j.ecoinf.2021.101351)):
un sistema de ecuaciones diferenciales con compartimentos de huevo, larva,
pupa y adulto, forzado día a día por precipitación, temperatura y humedad
relativa. De la salida diaria del modelo (número de huevos predichos) se arma el
**índice de oviposición**: cada valor se estandariza entre 0 y 1 contra
una ventana móvil de los 365 días previos, de modo que el índice mide qué
tan alta es la oviposición de hoy *en relación al último año en ese mismo
lugar*, no en términos absolutos.

**2. Modelo espacial (MCDA, análisis multicriterio).** Combina cuatro
capas raster semanales, procesadas en GRASS GIS (proyección POSGAR 2007 /
Argentina 4, EPSG:5346): NDVI de vegetación (Sentinel-2), altura de
construcciones, población y NBI (necesidades básicas insatisfechas). La
capa de construcciones combina los footprints de edificios de Open
Buildings con un modelo digital de elevación (DEM del IGN) y FABDEM (un
DEM "desnudo", sin edificios ni vegetación): la diferencia entre ambos
aproxima la altura de cada construcción. El resultado final es un raster
de **idoneidad de hábitat**: qué tan favorable es cada píxel para que el
mosquito complete su ciclo, independientemente de si hay huevos siendo
puestos esa semana o no.

**Índice de actividad final**, por píxel y por semana:

`índice de actividad = idoneidad espacial × índice de oviposición diario`
(promediado sobre los 7 días de la semana; el desvío intra-semanal queda
disponible como capa de error, σ, en el mapa).

### Clasificación en 4 categorías

Los mapas no muestran el índice crudo (0 a 1) sino 4 categorías (baja,
media, alta, muy alta), calibradas con datos reales de ovitrampas de cada
localidad, no con una escala arbitraria: el piso de "media" es el umbral
de Youden propio de esa localidad (el punto de corte que mejor separa
positivo/negativo en la curva ROC contra las ovitrampas), y los cortes
entre media/alta/muy alta son los terciles de los valores reales de
ovitrampa que superan ese umbral. Por eso el mismo valor de índice puede
caer en una categoría distinta según la localidad.

### Qué se proyecta a futuro y qué no

El índice de oviposición incorpora pronóstico meteorológico (NOAA CFS, 14
días) además del dato observado, así que su gráfico muestra un tramo a
futuro. El índice de actividad espacial, en cambio, depende de imágenes
satelitales reales (no hay forma de "pronosticar" una imagen Sentinel-2),
así que es siempre retrospectivo: la semana más reciente que se muestra es
siempre una semana ya transcurrida.

### Actualización automática

Todos los martes (`orquestador/`) el sistema re-descarga clima,
re-corre el modelo temporal, incorpora la semana de vegetación más
reciente que haya disponible por satélite, y recalcula MCDA e índice de
actividad. Si algún paso falla esa semana, el resto de la cadena sigue
corriendo igual con lo que haya disponible, y el dashboard avisa arriba de
todo si algo quedó desactualizado.

### Fuentes de datos

- **Precipitación**: NASA GES DISC,
  [GPM IMERG Late](https://gpm.nasa.gov/data/imerg) (diario, 0.1°)
- **Temperatura / humedad**: [NCEP](https://www.ncep.noaa.gov/) GDAS/FNL
- **Pronóstico**: NOAA CFS, vía
  [NOMADS](https://nomads.ncep.noaa.gov/)
- **Vegetación**: [Copernicus Sentinel-2](https://dataspace.copernicus.eu/)
  L2A (NDVI), descargado con
  [EODAG](https://github.com/CS-SI/eodag)
- **Población**: [WorldPop](https://www.worldpop.org/)
- **NBI**: [INDEC](https://www.indec.gob.ar/)
- **Construcciones**: footprints de
  [Open Buildings](https://sites.research.google/gr/open-buildings/) (Google),
  altura por diferencia entre el DEM del
  [IGN](https://www.ign.gob.ar/) y
  [FABDEM](https://www.fathom.global/product/fabdem/) (Fathom / Universidad
  de Bristol)
- **Mapas base**: [Esri](https://www.arcgis.com/) World Light Gray Canvas
  (HERE, Garmin, FAO, NOAA, USGS) y Esri World Imagery (Maxar, Earthstar
  Geographics)
        """
    )
    st.divider()
    st.markdown(footer_html(), unsafe_allow_html=True)
