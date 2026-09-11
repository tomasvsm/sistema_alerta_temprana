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
from rasterio.features import geometry_mask as rio_geometry_mask
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_geom as rio_transform_geom
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
EJIDOS_PATH = REPO_ROOT / "espacializacion" / "resources" / "ejidos" / "ejidos_4loc.geojson"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

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
# Cordoba corregida a 0.1632 el 2026-09-11 (antes 0.2360): se confirmo con
# los directores que el ciclo 2019-2020 (jun-dic 2019, temporada truncada)
# tuvo diferencias metodologicas reales en la toma de datos de ovitrampas,
# no solo el problema estadistico de evaluar una temporada incompleta. Se
# excluyo ese tramo del calculo -- ver
# validacion/cruce/analisis_correlacion_final.ipynb (notebook final, la
# original analisis_correlacion.ipynb queda como registro del diagnostico
# que llevo a la exclusion).
YOUDEN = {"1252": 0.1945, "1271": 0.0829, "1300": 0.3736, "1385": 0.1632}

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
    "1385": (0.336, 0.475),
}
PALETA = ["#2b83ba", "#83c1ab", "#e0f3b5", "#d7191c"]
CATEGORIAS = ["Actividad baja", "Actividad media", "Actividad alta", "Actividad muy alta"]

# Techo fijo de la escala de la capa de error (sigma): tiene que ser el
# mismo en todas las semanas para que los colores sean comparables entre
# si (si se recalculara por raster, la misma sigma real se veria distinta
# segun la semana). 0.15 cubre comodo el maximo real observado en los 244
# rasters de sigma existentes (0.129).
VMAX_SIGMA = 0.15

# El dashboard es un contenedor persistente (docker run -d --restart
# unless-stopped) que el orquestador NUNCA reinicia despues de la
# corrida semanal -- st.cache_data sin ttl se queda con los datos de la
# primera vez que se llamo cada funcion para siempre, y el dashboard
# podia quedar mostrando la semana vieja indefinidamente hasta un
# reinicio manual. 1h alcanza sobra para notar una corrida nueva sin
# recalcular todo en cada rerun.
CACHE_TTL = "1h"

# El CSV meteorologico arranca en 2023 (dos anios de spinup que necesita
# el modelo poblacional antes de la primera fecha real), pero el resto
# del sistema (indice de actividad, idoneidad) recien tiene datos desde
# que arranco la espacializacion. Se recorta el grafico a esa fecha para
# no mostrar dos anios de datos que ningun otro panel tiene y aligerar
# el render.
FECHA_INICIO_ESPACIALIZACION = pd.Timestamp("2025-01-14")


def bounds_categoricos(gid: str) -> list[float]:
    q33, q66 = TERCILES_CAMPO[gid]
    return [0.0, YOUDEN[gid], q33, q66, 1.0]


def codigo_y_valor_categoria_maxima(gid: str, arr: np.ndarray) -> tuple[int, float]:
    """Categoria MAS ALTA presente entre los pixeles validos de esta
    semana (-1, nan si no hay datos) y el valor Rw de ese pixel, no la mas
    frecuente: un solo pixel en una categoria superior ya sube el
    semaforo a ese nivel. Criterio de alerta temprana -- mas sensible que
    el promedio/moda, que casi siempre daria "baja" porque la mayoria del
    area esta genuinamente baja la mayor parte del tiempo (ver hallazgo
    del 2026-09-01)."""
    validos = arr[~np.isnan(arr)]
    if validos.size == 0:
        return -1, float("nan")
    valor_max = float(validos.max())
    codigo = int(np.digitize([valor_max], bounds_categoricos(gid)[1:-1])[0])
    return codigo, valor_max


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


class ControlNorte(MacroElement):
    """Flecha de norte minimalista (Leaflet no trae una propia) -- icono
    suelto sin recuadro/marco (no es un boton, no tiene click), con un
    leve halo blanco para que se lea bien sobre cualquier fondo del
    mapa base. Como L.Control propio, se apila solo debajo del selector
    de capas en la esquina superior derecha, sin pisarlo."""

    _template = Template("""
        {% macro script(this, kwargs) %}
        (function() {
            var mapa = {{ this._parent.get_name() }};
            var ControlIcono = L.Control.extend({
                options: {position: 'topright'},
                onAdd: function() {
                    var caja = L.DomUtil.create('div', '');
                    caja.style.cssText = 'width:22px;height:20px;display:flex;' +
                        'align-items:center;justify-content:center;pointer-events:none;';
                    caja.innerHTML =
                        '<svg width="16" height="20" viewBox="0 0 16 20" ' +
                        'style="filter:drop-shadow(0 0 1.5px white) drop-shadow(0 0 1.5px white) ' +
                        'drop-shadow(0 0 1.5px white);">' +
                        '<text x="8" y="7" text-anchor="middle" font-size="7" ' +
                        'font-family="sans-serif" font-weight="600" fill="#333">N</text>' +
                        '<polygon points="8,9 4,18 8,15" fill="#333"/>' +
                        '<polygon points="8,9 12,18 8,15" fill="white" stroke="#333" ' +
                        'stroke-width="0.75" stroke-linejoin="round"/>' +
                        '</svg>';
                    return caja;
                },
            });
            mapa.addControl(new ControlIcono());
        })();
        {% endmacro %}
    """)

    def __init__(self):
        super().__init__()
        self._name = "ControlNorte"


class ControlEscala(MacroElement):
    """Barra de escala solo metrica (Leaflet trae metrica+imperial por
    defecto via folium control_scale=True, pero imperial no aporta nada
    aca y suma ruido visual). Tipografia mas fina que la que trae
    Leaflet por defecto (heredada del resto del dashboard, no la
    generica del navegador)."""

    _template = Template("""
        {% macro script(this, kwargs) %}
        (function() {
            var escala = L.control.scale({imperial: false, position: 'bottomleft'})
                .addTo({{ this._parent.get_name() }});
            var linea = escala.getContainer().querySelector('.leaflet-control-scale-line');
            if (linea) {
                linea.style.cssText += 'border-color:rgba(0,0,0,0.45); ' +
                    'background:rgba(255,255,255,0.8); ' +
                    'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; ' +
                    'font-size:10px; font-weight:400; color:#444; padding:1px 5px;';
            }
        })();
        {% endmacro %}
    """)

    def __init__(self):
        super().__init__()
        self._name = "ControlEscala"


class ControlCoordenadas(MacroElement):
    """Coordenadas del cursor, con la misma tipografia y tamaño que
    ControlEscala (el plugin folium.plugins.MousePosition trae su propia
    tipografia fija, mucho mas grande, y no se puede reducir via sus
    parametros -- por eso un control propio en vez de ese plugin,
    ademas de evitar sumar una dependencia JS/CSS externa via CDN)."""

    _template = Template("""
        {% macro script(this, kwargs) %}
        (function() {
            var mapa = {{ this._parent.get_name() }};
            var estiloTexto = 'background:rgba(255,255,255,0.8); padding:1px 5px; ' +
                'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; ' +
                'font-size:10px; font-weight:400; color:#444; white-space:nowrap;';
            var ControlCoords = L.Control.extend({
                options: {position: 'bottomright'},
                onAdd: function() {
                    var caja = L.DomUtil.create('div', '');
                    caja.style.cssText = estiloTexto;
                    caja.innerHTML = '&nbsp;';
                    return caja;
                },
            });
            var control = new ControlCoords();
            mapa.addControl(control);
            var elemento = control.getContainer();
            // pegado al borde -- Leaflet le agrega la clase
            // "leaflet-control" solo, que trae 10px de margin-right y
            // margin-bottom via CSS; hay que pisarlo inline en el
            // propio elemento, no alcanza con el contenedor padre.
            elemento.style.margin = '0';
            mapa.on('mousemove', function(e) {
                elemento.innerHTML = e.latlng.lat.toFixed(3) + ' · ' + e.latlng.lng.toFixed(3);
            });
            mapa.on('mouseout', function() { elemento.innerHTML = '&nbsp;'; });
        })();
        {% endmacro %}
    """)

    def __init__(self):
        super().__init__()
        self._name = "ControlCoordenadas"


class RecalcularAlMostrar(MacroElement):
    """Mismo hook de invalidateSize() al imprimir que ControlRecentrar,
    sin el boton de recentrar -- para los mapas chicos/de referencia
    (variables estaticas). Ademas corrige un problema especifico de estos
    mapas: como viven dentro de un expander colapsado por defecto
    ("Variables espaciales"), su iframe arranca oculto/0x0, y
    fitBounds() calculado en ese momento cae al zoom minimo (mapa del
    mundo entero) -- ese calculo no se repite solo al abrir el expander
    despues. Un ResizeObserver sobre el propio contenedor del mapa
    detecta el momento en que pasa a tener tamaño real y ahi si hace
    invalidateSize()+fitBounds()."""

    _template = Template("""
        {% macro script(this, kwargs) %}
        (function() {
            var mapa = {{ this._parent.get_name() }};
            var limites = L.latLngBounds({{ this.bounds }});
            var yaAjustado = false;
            function ajustar() {
                var tam = mapa.getSize();
                if (tam.x > 0 && tam.y > 0 && !yaAjustado) {
                    yaAjustado = true;
                    mapa.invalidateSize();
                    mapa.fitBounds(limites);
                }
            }
            new ResizeObserver(ajustar).observe(mapa.getContainer());
            window.matchMedia('print').addEventListener('change', function() {
                mapa.invalidateSize();
            });
            window.addEventListener('beforeprint', function() { mapa.invalidateSize(); });
        })();
        {% endmacro %}
    """)

    def __init__(self, bounds):
        super().__init__()
        self._name = "RecalcularAlMostrar"
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


def figura_semaforo_gauge(gid: str, codigo_activo: int, valor_activo: float) -> go.Figure:
    """Reemplazo del semaforo de 4 cajas: un velocimetro con aguja (sugerido
    por los directores de tesis), armado con go.Pie en dona (go.Indicator
    no tiene una aguja de verdad, solo una marca radial fina) siguiendo el
    patron: mitad inferior oculta + mitad superior con los 4 segmentos +
    figuras de forma "line"/"circle" para la aguja y el pivote.

    Los 4 segmentos se dibujan del MISMO ancho visual (no proporcional al
    ancho real en Rw de cada categoria) -- con los anchos reales, "muy
    alta" ocupa mas de medio semicirculo y el resto queda comprimido,
    ilegible. La aguja sigue reflejando el valor real: se reescala la
    posicion del valor DENTRO de su categoria (Youden y los 2 terciles de
    bounds_categoricos, la misma calibracion de siempre) al cuarto de
    circulo parejo que le toca a esa categoria."""
    bounds = bounds_categoricos(gid)
    if codigo_activo < 0 or np.isnan(valor_activo):
        codigo_activo, valor_activo = 0, 0.0

    n = 4
    b_ini, b_fin = bounds[codigo_activo], bounds[codigo_activo + 1]
    frac_en_categoria = 0.0 if b_fin == b_ini else (valor_activo - b_ini) / (b_fin - b_ini)
    frac_en_categoria = min(max(frac_en_categoria, 0.0), 1.0)
    fraccion_total = (codigo_activo + frac_en_categoria) / n

    # hand_angle: pi (izquierda, fraccion=0) -> 0 (derecha, fraccion=1),
    # recorriendo el semicirculo superior en sentido horario.
    hand_angle = np.pi * (1 - fraccion_total)
    largo_aguja = 0.42

    etiquetas = [c.replace("Actividad ", "") for c in CATEGORIAS]
    colores_texto = [_texto_legible_sobre(c) for c in PALETA]
    fig = go.Figure(
        data=[go.Pie(
            values=[0.5] + [0.5 / n] * n,
            rotation=90,
            hole=0.55,
            direction="clockwise",
            sort=False,
            marker=dict(colors=["rgba(0,0,0,0)"] + PALETA, line=dict(width=0)),
            text=[""] + [f"<b>{e}</b>" for e in etiquetas],
            textinfo="text",
            textfont=dict(color=["rgba(0,0,0,0)"] + colores_texto, size=12),
            hoverinfo="skip",
        )],
        layout=go.Layout(
            showlegend=False,
            margin=dict(t=28, b=0, l=10, r=10),
            height=170,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            annotations=[go.layout.Annotation(
                text="NIVEL DE ACTIVIDAD DE ESTA SEMANA",
                font=dict(size=11.5, color="rgba(49,51,63,0.65)"),
                x=0.5, xanchor="center", xref="paper",
                y=1.12, yanchor="bottom", yref="paper",
                showarrow=False,
            )],
            shapes=[
                go.layout.Shape(
                    type="line", xref="paper", yref="paper",
                    x0=0.5, y0=0.5,
                    x1=0.5 + largo_aguja * np.cos(hand_angle),
                    y1=0.5 + largo_aguja * np.sin(hand_angle),
                    line=dict(color="#1a1a1a", width=4),
                ),
                go.layout.Shape(
                    type="circle", xref="paper", yref="paper",
                    x0=0.47, x1=0.53, y0=0.47, y1=0.53,
                    fillcolor="#1a1a1a", line_color="#1a1a1a",
                ),
            ],
        ),
    )
    return fig


@st.cache_data(ttl=CACHE_TTL)
def semanas_disponibles(gid: str) -> list[str]:
    patron = re.compile(rf"^(\d{{4}}-\d{{2}}-\d{{2}})_{gid}_indice_actividad\.tif$")
    fechas = [m.group(1) for f in IA_DIR.iterdir() if (m := patron.match(f.name))]
    return sorted(fechas, reverse=True)


@st.cache_data
def cargar_ejidos() -> dict[str, dict]:
    """gid -> geometria GeoJSON (EPSG:4326) del limite administrativo real
    (ejido) de esa localidad -- las ROI usadas para procesar son
    cuadrados/rectangulos de buffer, pero el limite real es irregular (ver
    manuscrito/figuras/mapa-localidades-ovis.png), asi que todo raster se
    enmascara contra este poligono antes de mostrarse."""
    if not EJIDOS_PATH.exists():
        return {}
    with open(EJIDOS_PATH) as f:
        data = json.load(f)
    return {feat["properties"]["gid"]: feat["geometry"] for feat in data["features"]}


@st.cache_data(ttl=CACHE_TTL)
def _mascara_ejido(gid: str, shape: tuple[int, int], transform_coefs: tuple, crs_str: str) -> np.ndarray:
    """Mascara booleana (True = dentro del ejido) rasterizada al grid
    puntual de un raster -- no todos los rasters de un mismo gid comparten
    grid exacto (las estaticas usan uno levemente distinto al del indice
    de actividad/MCDA/NDVI), asi que se rasteriza por raster."""
    ejidos = cargar_ejidos()
    if gid not in ejidos:
        return np.ones(shape, dtype=bool)
    geom_local = rio_transform_geom("EPSG:4326", crs_str, ejidos[gid])
    transform = rasterio.Affine(*transform_coefs)
    return rio_geometry_mask([geom_local], out_shape=shape, transform=transform, invert=True)


def enmascarar_por_ejido(arr: np.ndarray, gid: str, transform, crs) -> np.ndarray:
    mascara = _mascara_ejido(gid, arr.shape, tuple(transform)[:6], crs.to_string())
    return np.where(mascara, arr, np.nan)


@st.cache_data(ttl=CACHE_TTL)
def cargar_raster_4326(path: str, gid: str | None = None):
    """Reproyecta a EPSG:4326 y devuelve (array, bounds) listos para folium."""
    with rasterio.open(path) as src:
        with WarpedVRT(src, crs="EPSG:4326", resampling=rasterio.enums.Resampling.nearest) as vrt:
            arr = vrt.read(1)
            bounds = vrt.bounds
            nodata = vrt.nodata
            transform = vrt.transform
            crs = vrt.crs
    arr = np.where(arr == nodata, np.nan, arr)
    if gid is not None:
        arr = enmascarar_por_ejido(arr, gid, transform, crs)
    return arr, [[bounds.bottom, bounds.left], [bounds.top, bounds.right]]


@st.cache_data(ttl=CACHE_TTL)
def contorno_roi_4326(gid: str) -> list[list[list[float]]]:
    """Anillos (poligono principal + eventuales islas, ej. Villa Maria) del
    limite real del ejido de la localidad -- no la ROI cuadrada usada para
    procesar -- reproyectados a EPSG:4326 listos para folium.PolyLine."""
    geom = cargar_ejidos().get(gid)
    if geom is None:
        return []
    if geom["type"] == "Polygon":
        anillos_coords = geom["coordinates"]
    else:  # MultiPolygon
        anillos_coords = [anillo for poligono in geom["coordinates"] for anillo in poligono]
    return [[[lat, lon] for lon, lat in anillo] for anillo in anillos_coords]


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


def raster_a_imagen_rgba_viridis5(arr: np.ndarray) -> np.ndarray:
    """RGBA para las variables categoricas de 5 clases del MCDA (valores
    0/0.25/0.5/0.75/1, paleta Viridis-5), con nodata/fuera-de-ejido
    transparente -- para mostrarlas sobre un mapa base real (Folium) en
    vez de flotar sobre fondo blanco."""
    codigo = np.clip(np.round(np.nan_to_num(arr, nan=0.0) * 4), 0, len(PALETA_VIRIDIS5) - 1)
    cmap = mcolors.ListedColormap(PALETA_VIRIDIS5)
    rgba = cmap(codigo.astype(int))
    rgba[np.isnan(arr), 3] = 0.0
    return (rgba * 255).astype(np.uint8)


@st.cache_data(ttl=CACHE_TTL)
def cargar_raster_nativo(path: str, gid: str | None = None) -> np.ndarray:
    """Lee el raster en su CRS original (5346), sin reproyectar -- para
    graficos estaticos (matplotlib) que no van sobre un mapa base."""
    with rasterio.open(path) as src:
        arr = src.read(1).astype(np.float32)
        nodata = src.nodata
        transform = src.transform
        crs = src.crs
    arr = np.where(arr == nodata, np.nan, arr)
    if gid is not None:
        arr = enmascarar_por_ejido(arr, gid, transform, crs)
    return arr


@st.cache_data(ttl=CACHE_TTL)
def serie_temporal_indice_actividad(gid: str) -> pd.DataFrame:
    """Promedio y maximo espacial del indice de actividad, por semana."""
    filas = []
    for fecha in semanas_disponibles(gid):
        arr = cargar_raster_nativo(str(IA_DIR / f"{fecha}_{gid}_indice_actividad.tif"), gid=gid)
        if np.all(np.isnan(arr)):
            continue
        filas.append({"date": fecha, "media": np.nanmean(arr), "maximo": np.nanmax(arr)})
    if not filas:
        return pd.DataFrame(columns=["date", "media", "maximo"])
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
# Las 4 variables estaticas + NDVI comparten esta codificacion cruda de
# pixel (0, 0.25, 0.5, 0.75, 1), en el mismo orden que sus listas de
# categorias de arriba -- ver caja_leyenda_html.
VALORES_CATEGORIA_5 = ["0", "0.25", "0.5", "0.75", "1"]


@st.cache_data(ttl=CACHE_TTL)
def cargar_variable_estatica_4326(gid: str, variable: str):
    """Reproyectada a EPSG:4326 (array, bounds) para mostrarse sobre un
    mapa base real (Folium), igual que el indice de actividad."""
    subdir, sufijo, _, _ = VARIABLES_ESTATICAS[variable]
    ruta = ESTATICAS_DIR / f"gid_{gid}_estaticas" / subdir / f"gid_{gid}_estaticas_{sufijo}.tif"
    if not ruta.exists():
        return None, None
    return cargar_raster_4326(str(ruta), gid=gid)


def mapa_folium_compacto(arr: np.ndarray, bounds, gid: str) -> folium.Map:
    """Mini mapa Leaflet estatico (sin zoom/paneo manual, sin controles)
    para las variables de referencia de Variables espaciales -- mismo
    basemap claro que el indice de actividad, para que no queden
    "flotando" sobre fondo blanco sin contexto geografico."""
    centro = [(bounds[0][0] + bounds[1][0]) / 2, (bounds[0][1] + bounds[1][1]) / 2]
    m = folium.Map(location=centro, tiles=None, zoom_control=False, attributionControl=False)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/"
              "Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        attr="Esri",
    ).add_to(m)
    folium.raster_layers.ImageOverlay(
        image=raster_a_imagen_rgba_viridis5(arr), bounds=bounds, opacity=0.85,
    ).add_to(m)
    for anillo in contorno_roi_4326(gid):
        folium.PolyLine(locations=anillo, color="#8a8a8a", weight=1, opacity=0.7).add_to(m)
    m.fit_bounds(bounds)
    RecalcularAlMostrar(bounds).add_to(m)
    return m


def caja_leyenda_html(
    titulo: str, colores: list[str], etiquetas: list[str], valores: list[str] | None = None
) -> str:
    """Recuadro de referencia con titulo y una fila por categoria (cuadrado
    de color + texto) -- mismo estilo que las leyendas de
    manuscrito/figuras/variables*.png, en vez de un listado de texto
    suelto. Si se pasan valores (el codigo crudo del pixel, ej. "0.25"),
    se muestran como "valor (significado)"."""
    if valores is not None:
        etiquetas = [f"{v} ({e})" for v, e in zip(valores, etiquetas)]
    filas = "".join(
        '<div style="display:flex; align-items:center; gap:6px; margin:2px 0;">'
        f'<span style="width:12px; height:12px; border-radius:2px; background:{c}; '
        'display:inline-block; flex-shrink:0;"></span>'
        f'<span>{e}</span></div>'
        for c, e in zip(colores, etiquetas)
    )
    return (
        '<div style="border:1px solid rgba(128,128,128,0.35); border-radius:8px; '
        'padding:8px 12px; display:inline-block; font-size:0.8rem; background:white;">'
        f'<div style="font-weight:600; margin-bottom:6px; font-size:0.72rem; '
        f'text-transform:uppercase; letter-spacing:0.03em; opacity:0.7;">{titulo}</div>'
        f'{filas}</div>'
    )


@st.cache_data(ttl=CACHE_TTL)
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


@st.cache_data(ttl=CACHE_TTL)
def figura_estatica_vegetacion(gid: str, fecha: str) -> go.Figure:
    arr = cargar_raster_nativo(vegetacion_disponible(gid)[fecha], gid=gid)
    codigo = np.round(arr * 4)
    fig = px.imshow(
        codigo,
        color_continuous_scale=_colorscale_escalonada(PALETA_VIRIDIS5),
        range_color=[0, 5], aspect="equal",
    )
    fig.update_traces(hoverinfo="skip", hovertemplate=None)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(
        # aspect="equal" fuerza el raster (cuadrado) a ocupar un cuadrado
        # centrado dentro del lienzo -- height tiene que quedar parecido
        # al width (230, ver st.plotly_chart) para que el cuadrado llene
        # el lienzo en vez de dejar franjas en blanco arriba/abajo.
        height=230, margin=dict(t=10, b=10, l=10, r=10), coloraxis_showscale=False,
    )
    return fig


@st.cache_data(ttl=CACHE_TTL)
def semanas_idoneidad_disponibles(gid: str) -> list[str]:
    patron = re.compile(rf"^(\d{{4}}-\d{{2}}-\d{{2}})_{gid}_MCDA\.tif$")
    if not MCDA_DIR.is_dir():
        return []
    fechas = [m.group(1) for f in MCDA_DIR.iterdir() if (m := patron.match(f.name))]
    return sorted(fechas)


@st.cache_data(ttl=CACHE_TTL)
def figura_estatica_idoneidad(gid: str, fecha: str) -> go.Figure:
    """Idoneidad (MCDA) de una semana puntual, categorizada en 4 clases
    lineales (cuartos del rango [0,1]) y con los mismos colores que el
    indice de actividad -- pero NO con los cortes de Youden/terciles de
    bounds_categoricos(), que estan calibrados contra el indice de
    actividad real (idoneidad x oviposicion, con fuerte estacionalidad
    por el piso de oviposicion). El MCDA solo no tiene ese factor
    estacional y su rango se mantiene medio-alto casi todo el año en
    zona urbana, asi que esos cortes lo pintaban casi todo como
    "alta"/"muy alta"."""
    arr = cargar_raster_nativo(str(MCDA_DIR / f"{fecha}_{gid}_MCDA.tif"), gid=gid)
    codigo = np.digitize(arr, [0.25, 0.5, 0.75]).astype(float)
    codigo[np.isnan(arr)] = np.nan
    fig = px.imshow(
        codigo,
        color_continuous_scale=_colorscale_escalonada(PALETA),
        range_color=[0, len(PALETA)], aspect="equal",
    )
    fig.update_traces(hoverinfo="skip", hovertemplate=None)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(
        # aspect="equal" fuerza el raster (cuadrado) a ocupar un cuadrado
        # centrado dentro del lienzo -- height tiene que quedar parecido
        # al width (500, ver st.plotly_chart) para que el cuadrado llene
        # el lienzo en vez de dejar franjas en blanco arriba/abajo.
        height=380, coloraxis_showscale=False, margin=dict(t=10, b=10, l=10, r=10),
    )
    return fig


@st.cache_data(ttl=CACHE_TTL)
def cargar_indice_oviposicion(gid: str) -> pd.DataFrame | None:
    nombre = GID_SNAKE[gid]
    candidatos = sorted(MODELO_DIR.glob(f"*_{gid}_{nombre}_indice_oviposicion.csv"))
    if not candidatos:
        return None
    df = pd.read_csv(candidatos[-1], parse_dates=["date"])
    return df


@st.cache_data(ttl=CACHE_TTL)
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
    hoy = fecha_referencia()
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


def fecha_referencia() -> pd.Timestamp:
    """Fecha de referencia para separar "confirmado" de "pronosticado"
    en los graficos: NO es el reloj del sistema (date.today() puede caer
    cualquier dia de la semana) sino la fecha de la ultima corrida
    semanal del orquestador (fecha_ref en estado_ultima_corrida.json) --
    el pipeline corre una vez por semana, asi que "hoy" para los datos
    casi siempre es unos dias anterior al dia real en que se abre el
    dashboard."""
    estado = cargar_estado_orquestador()
    if estado is None:
        return pd.Timestamp(date.today())
    return pd.Timestamp(estado["fecha_ref"])


# --------------------------------------------------------------------------
st.set_page_config(page_title="Alerta temprana Aedes aegypti", layout="wide")
st.markdown(
    """<style>
    header[data-testid="stHeader"] { background: transparent; height: 2.5rem; }
    div[data-testid="stAppDeployButton"] { display: none; }
    .block-container { padding-top: 0.8rem; }
    div[data-testid="stHeading"]:has(h1) { text-align: center; }
    div[data-testid="stHeading"] h1 { font-size: 2rem; padding: 0.3rem 0 0.5rem; }
    div[data-testid="stSlider"] { margin: -10px 0 -8px 0; }
    div[data-testid="stLayoutWrapper"]:has(.st-key-mapa_centrado) { align-self: center; }
    div[data-testid="stLayoutWrapper"]:has([class*="st-key-var_"]) { align-self: center; }
    div[data-testid="stLayoutWrapper"]:has(.st-key-semaforo_centrado) { align-self: center; }

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
        /* stExpander NO entra en esta lista a proposito: el expander de
           idoneidad+variables mide varias hojas completas (5 mapas), asi
           que pedirle que evite cortarse es una condicion imposible de
           cumplir. Chrome, al no poder cumplirla, empuja el bloque entero
           a la pagina siguiente y deja la anterior casi en blanco --
           bloqueado a que cada mapa individual (stVerticalBlock /
           stHorizontalBlock / stElementContainer) no se corte alcanza y
           deja que el expander como un todo se parta entre esos mapas. */
        div[data-testid="stElementContainer"],
        div[data-testid="stHorizontalBlock"],
        div[data-testid="stVerticalBlock"],
        div[data-testid="stCustomComponentV1"],
        div[data-testid="stPlotlyChart"] {
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
    arr_ia, bounds = cargar_raster_4326(str(ia_path), gid=gid)
    # El semaforo usa el raster nativo (mismo que serie_temporal_indice_
    # actividad para el "maximo espacial") en vez del reproyectado a
    # 4326 -- cada uno aplica su propia mascara de ejido por separado
    # (distinto shape/transform), y podian discrepar levemente en
    # pixeles del borde del ejido si se mezclaban las dos fuentes.
    arr_ia_nativo = cargar_raster_nativo(str(ia_path), gid=gid)
    codigo_activo, valor_activo = codigo_y_valor_categoria_maxima(gid, arr_ia_nativo)

    with col_semaforo:
        with st.container(width=300, key="semaforo_centrado"):
            st.plotly_chart(figura_semaforo_gauge(gid, codigo_activo, valor_activo), width=300)

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

            m = folium.Map(location=centro, tiles=None, attributionControl=False)
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

            for anillo in contorno_roi_4326(gid):
                folium.PolyLine(
                    locations=anillo, color="#8a8a8a", weight=1.2, opacity=0.8,
                ).add_to(m)

            if sigma_path.exists():
                arr_sigma, bounds_sigma = cargar_raster_4326(str(sigma_path), gid=gid)
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
            ControlNorte().add_to(m)
            ControlEscala().add_to(m)
            ControlCoordenadas().add_to(m)

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

    EXP_SERIE_KEY = "exp_serie_temporal"
    with st.expander(
        "Evolución del índice de actividad (serie temporal)",
        key=EXP_SERIE_KEY, on_change="rerun",
    ):
        if st.session_state.get(EXP_SERIE_KEY):
            df_serie = serie_temporal_indice_actividad(gid)
            if df_serie.empty:
                st.info("Sin semanas suficientes para mostrar evolución.")
            else:
                fig_serie = go.Figure()
                fig_serie.add_trace(go.Scatter(
                    x=df_serie["date"], y=df_serie["media"],
                    mode="lines+markers", name="Media espacial", line=dict(color="#d7191c"),
                    hovertemplate="Semana: %{x|%d/%m/%Y}<br>Media: %{y:.3f}<extra></extra>",
                ))
                fig_serie.add_trace(go.Scatter(
                    x=df_serie["date"], y=df_serie["maximo"],
                    mode="lines", name="Máximo espacial",
                    line=dict(color="#d7191c", dash="dot", width=1),
                    hovertemplate="Semana: %{x|%d/%m/%Y}<br>Máximo: %{y:.3f}<extra></extra>",
                ))
                fig_serie.update_layout(
                    height=420, margin=dict(t=30, b=10, l=10, r=10),
                    yaxis_title="Índice de actividad", xaxis_title=None,
                    legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0.5, xanchor="center"),
                    xaxis=dict(tickformatstops=TICKFORMATSTOPS_FECHA),
                    # asi la fecha exacta de la semana se ve al pasar el
                    # mouse aunque los ticks del eje, a este zoom, solo
                    # alcancen a mostrar mes/año (no distinguen semana).
                    hovermode="x unified",
                )
                st.plotly_chart(fig_serie, width=950)

    # st.expander no evita que su contenido se calcule y se mande al
    # navegador cuando esta colapsado -- el codigo de adentro corre en
    # cada rerun igual. Ese contenido incluye 4 mapas folium (cada uno un
    # iframe que baja Leaflet/jQuery/Bootstrap/FontAwesome desde CDNs
    # externos y pide tiles a ArcGIS) mas dos animaciones Plotly de ~90
    # cuadros, todo eso en CADA cambio de localidad o semana aunque el
    # usuario nunca haya abierto el panel. Por eso se usa `key=` para leer
    # el estado abierto/cerrado y recien construir el contenido si esta
    # realmente expandido.
    EXP_IDONEIDAD_KEY = "exp_idoneidad_variables"
    with st.expander(
        "Índice de idoneidad de hábitat y variables espaciales utilizadas",
        key=EXP_IDONEIDAD_KEY, on_change="rerun",
    ):
        if st.session_state.get(EXP_IDONEIDAD_KEY):
            st.markdown("**Índice de idoneidad de hábitat**")
            fechas_idoneidad = semanas_idoneidad_disponibles(gid)
            if not fechas_idoneidad:
                st.info("Sin datos de idoneidad para esta localidad.")
            else:
                with st.container(width=560, key="idoneidad_centrado"):
                    key_semana_idoneidad = f"semana_idoneidad_{gid}"
                    st.select_slider(
                        "Recorrer semanas", options=fechas_idoneidad,
                        value=fechas_idoneidad[-1], key=key_semana_idoneidad,
                        label_visibility="collapsed", format_func=fmt_fecha,
                    )
                    with st.container(horizontal=True, vertical_alignment="center"):
                        fecha_idoneidad_sel = st.session_state[key_semana_idoneidad]
                        st.plotly_chart(
                            figura_estatica_idoneidad(gid, fecha_idoneidad_sel), width=380,
                        )
                        etiquetas_idoneidad = [c.replace("Actividad ", "") for c in CATEGORIAS]
                        st.markdown(
                            caja_leyenda_html("Idoneidad", PALETA, etiquetas_idoneidad),
                            unsafe_allow_html=True,
                    )

            st.divider()

            st.markdown("**Variables espaciales**")
            col_v1, col_v2, col_v3, col_v4 = st.columns(4)
            for col, variable in zip((col_v1, col_v2, col_v3), ("construcciones", "poblacion", "nbi")):
                with col:
                    arr_var, bounds_var = cargar_variable_estatica_4326(gid, variable)
                    _, _, titulo_var, etiquetas_var = VARIABLES_ESTATICAS[variable]
                    if arr_var is None:
                        st.info(f"Sin datos de {titulo_var.lower()} para esta localidad.")
                    else:
                        with st.container(width=230, key=f"var_{variable}"):
                            st.markdown(
                                f'<div style="text-align:center; font-weight:600; '
                                f'margin-bottom:2px;">{titulo_var}</div>',
                                unsafe_allow_html=True,
                            )
                            st_folium(
                                mapa_folium_compacto(arr_var, bounds_var, gid),
                                height=230, width=230, returned_objects=[],
                                key=f"folium_{gid}_{variable}",
                            )
                            st.markdown(
                                caja_leyenda_html(
                                    titulo_var, PALETA_VIRIDIS5, etiquetas_var,
                                    valores=VALORES_CATEGORIA_5,
                                ),
                                unsafe_allow_html=True,
                            )
            with col_v4:
                disponibles_veg = vegetacion_disponible(gid)
                if not disponibles_veg:
                    st.markdown("**Vegetación**")
                    st.info("Sin datos de vegetación para esta localidad.")
                else:
                    with st.container(width=230, key="var_vegetacion"):
                        st.markdown(
                            '<div style="text-align:center; font-weight:600; '
                            'margin-bottom:2px;">Vegetación</div>',
                            unsafe_allow_html=True,
                        )
                        fechas_veg = sorted(disponibles_veg.keys())
                        key_semana_veg = f"semana_vegetacion_{gid}"
                        st.select_slider(
                            "Recorrer semanas", options=fechas_veg,
                            value=fechas_veg[-1], key=key_semana_veg,
                            label_visibility="collapsed", format_func=fmt_fecha,
                        )
                        fecha_veg_sel = st.session_state[key_semana_veg]
                        st.plotly_chart(
                            figura_estatica_vegetacion(gid, fecha_veg_sel), width=230,
                        )
                        st.markdown(
                            caja_leyenda_html(
                                "Vegetación", PALETA_VIRIDIS5, CATEGORIAS_NDVI,
                                valores=VALORES_CATEGORIA_5,
                            ),
                            unsafe_allow_html=True,
                        )

    EXP_METEO_KEY = "exp_datos_meteorologicos"
    with st.expander("Datos meteorológicos", key=EXP_METEO_KEY, on_change="rerun"):
        if st.session_state.get(EXP_METEO_KEY):
            df_met = cargar_serie_meteorologica(gid)
            if df_met is None:
                st.info("Sin datos meteorológicos para esta localidad.")
            else:
                hoy = fecha_referencia()
                ventana = df_met[df_met["date"] >= FECHA_INICIO_ESPACIALIZACION]
                # el CSV del modelo trae tanto clima observado como
                # pronosticado (mismo archivo, sin columna que distinga uno
                # de otro) -- fin_pronost es simplemente la ultima fecha
                # disponible, que cae unos dias despues de hoy.
                fin_pronost = ventana["date"].max()

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
                if fin_pronost > hoy:
                    fig_met.add_vrect(x0=hoy, x1=fin_pronost, fillcolor="gray", opacity=0.08, line_width=0)
                    fig_met.add_annotation(
                        x=hoy + (fin_pronost - hoy) / 2, y=1, yref="paper", yanchor="top",
                        text="Pronóstico", showarrow=False, font=dict(size=10, color="gray"),
                    )
                fig_met.add_vline(x=hoy, line_dash="dot", line_color="gray")
                fig_met.update_layout(
                    height=350, margin=dict(t=50, b=10, l=10, r=10),
                    yaxis=dict(title="Precipitación (mm)"),
                    yaxis2=dict(title="°C / %", overlaying="y", side="right"),
                    legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0.5, xanchor="center"),
                    # Datos completos desde siempre en el grafico (autoscale
                    # los muestra), pero al abrir arranca con zoom al ultimo
                    # año hasta el final del pronostico -- el rango largo
                    # completo casi no se distingue, y antes se cortaba
                    # justo en "hoy" dejando afuera los dias pronosticados.
                    xaxis=dict(
                        tickformatstops=TICKFORMATSTOPS_FECHA,
                        range=[hoy - pd.Timedelta(days=365), fin_pronost + pd.Timedelta(days=1)],
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
*Aedes aegypti* en cuatro localidades de Córdoba: Córdoba capital, Río
Cuarto, Villa María y Salsipuedes. El índice de actividad final combina un
índice de oviposición (temporal, basado en datos meteorológicos) con un
índice de idoneidad de hábitat (espacial, MCDA), a una resolución espacial
de 100 m y con actualización semanal.

### Índice de oviposición

Se deriva de un modelo de dinámica poblacional de *Aedes aegypti* (huevo,
larva, pupa, adulto) forzado día a día por temperatura, humedad y
precipitación
([Aguirre et al., 2021](https://doi.org/10.1016/j.ecoinf.2021.101351)).
El número de huevos que predice el modelo para cada día se estandariza
contra su propia ventana móvil de 365 días previos:
"""
    )
    st.latex(
        r"IO_d = \frac{huevos_d - \min_{[d-365,\,d]}(huevos)}"
        r"{\max_{[d-365,\,d]}(huevos) - \min_{[d-365,\,d]}(huevos)}"
    )
    st.markdown(
        """
Un valor de 1 indica el máximo de oviposición del último año en esa
localidad, no una cantidad absoluta de huevos. Los siete valores diarios
de cada semana, cada uno con su propia ventana de 365 días, dan además un
desvío estándar intrasemanal que se reporta como incertidumbre del
índice.

Además del dato meteorológico observado, el modelo se corre con pronóstico
(CFS, NOAA) a 14 días como entrada, lo que proyecta el índice de
oviposición 14 días hacia adelante. El tramo a futuro del gráfico no es
una extrapolación estadística de la serie, sino la salida del mismo
modelo forzada con temperatura, humedad y precipitación pronosticadas en
vez de observadas. La idoneidad de hábitat y el índice de actividad, en
cambio, dependen de imágenes satelitales reales y son siempre
retrospectivos.

### Idoneidad de hábitat

Combina cuatro variables espaciales, cada una remuestreada a 100 m de
resolución y categorizada entre 0 y 1, mediante análisis multicriterio
(MCDA-AHP).
"""
    )
    st.image(
        str(ASSETS_DIR / "workflow_variables_espaciales.png"),
        caption="Cálculo y categorización de cada una de las cuatro variables espaciales.",
    )
    st.latex(
        r"MCDA(x,y) = W_{veg}\,V(x,y) + W_{per}\,P(x,y) "
        r"+ W_{soc}\,S(x,y) + W_{con}\,C(x,y)"
    )
    st.markdown(
        """
Los pesos se determinaron por comparación pareada (AHP) según dos
criterios: la disponibilidad de sitios de cría (contenedores artificiales)
y descanso, y la disponibilidad de fuentes de alimentación (sangre y
fluidos vegetales). Construcciones y NBI se asociaron a la disponibilidad
de sitios de cría: menor altura de edificación y mayor proporción de NBI
se asumen asociadas a más recipientes artificiales aptos como criadero.
La densidad poblacional se asoció a la disponibilidad de sangre. La
vegetación se consideró aportante a ambos criterios (sitio de descanso y
fuente de fluidos vegetales), lo que explica su peso dominante:

| Variable | Peso |
|---|---|
| Vegetación (NDVI) | 0.5596 |
| Población | 0.2495 |
| NBI | 0.0955 |
| Construcciones | 0.0955 |

La vegetación se actualiza semanalmente a partir de Sentinel-2; las otras
tres variables son estáticas entre semanas y se realinean a la grilla del
NDVI de cada semana por remuestreo de vecino más cercano.

### Índice de actividad

Combina los dos anteriores por píxel y por día. El índice de oviposición
tiene resolución diaria; el mapa de idoneidad se actualiza una vez por
semana junto con la vegetación, así que el resultado diario se promedia a
resolución semanal para el mapa final:
"""
    )
    st.latex(r"R_d(x,y) = MCDA_w(x,y) \times IO_d")
    st.markdown("Promediado sobre los 7 días de la semana:")
    st.latex(r"R_w(x,y) = \frac{1}{7}\sum_{d=1}^{7} R_d(x,y)")
    st.markdown(
        """
con el desvío estándar intrasemanal disponible como capa de error (σ) en
el mapa. Antes de la multiplicación se aplica un piso de 0.1 al índice de
oviposición diario, para que los períodos de mínima actividad predicha no
anulen la variabilidad espacial que aporta la idoneidad de hábitat.

### Cómo se leen los mapas

Los mapas no muestran el índice crudo sino 4 categorías: baja, media,
alta y muy alta. Los cortes entre categorías se calibraron contra datos
reales de ovitrampas de cada localidad, buscando el punto de corte que
mejor separa las semanas con presencia confirmada de las que no. Los
cortes no son iguales entre localidades, así que el mismo valor de índice
puede caer en una categoría distinta según el lugar.

El indicador "nivel de actividad de esta semana" no muestra un promedio
del mapa. Muestra la categoría más alta alcanzada por al menos un píxel
de la localidad esa semana. Es una decisión deliberada de alerta
temprana, más sensible que el promedio o la moda, que casi siempre darían
"baja" porque la mayor parte del área está en esa categoría la mayor
parte del tiempo.

### Actualización automática

El sistema se actualiza una vez por semana, todos los miércoles: descarga
datos meteorológicos nuevos con corte al martes anterior inclusive, vuelve
a correr el modelo de oviposición, incorpora la imagen satelital más
reciente disponible y recalcula la idoneidad y el índice de actividad. Si
algún paso falla esa semana, el resto sigue funcionando igual con lo que
haya disponible, y el dashboard avisa arriba de todo si algo quedó
desactualizado.
"""
    )
    st.image(
        str(ASSETS_DIR / "workflow_sistema.png"),
        caption="Cómo está armada la actualización semanal automática.",
    )
    st.markdown(
        """
### Fuentes de datos

- **Precipitación**: NASA,
  [GPM IMERG Late](https://www.earthdata.nasa.gov/data/catalog/ges-disc-gpm-3imergdl-07)
  (diario, 0.1°)
- **Temperatura y humedad**: NCEP,
  [GDAS/FNL](https://rda.ucar.edu/datasets/ds083.3/)
- **Pronóstico climático**: NOAA,
  [Climate Forecast System](https://www.ncei.noaa.gov/products/weather-climate-models/climate-forecast-system)
- **Vegetación**:
  [Copernicus Sentinel-2](https://dataspace.copernicus.eu/data-collections/copernicus-sentinel-missions/sentinel-2)
  L2A (NDVI)
- **Población**:
  [WorldPop](https://data.humdata.org/dataset/worldpop-population-counts-for-argentina)
- **NBI**:
  [INDEC](https://www.indec.gob.ar/indec/web/Nivel4-Tema-4-47-156)
- **Construcciones**: footprints de
  [Open Buildings](https://sites.research.google/gr/open-buildings/) (Google),
  altura por diferencia entre el
  [modelo digital de elevación del IGN](https://www.ign.gob.ar/NuestrasActividades/Geodesia/ModeloDigitalElevaciones/Introduccion)
  y [FABDEM](https://www.fathom.global/product/fabdem/) (Fathom / Universidad
  de Bristol)
- **Mapas base**: Esri World Light Gray Canvas y Esri World Imagery
        """
    )
    st.divider()
    st.markdown(footer_html(), unsafe_allow_html=True)
