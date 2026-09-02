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
from matplotlib import colors as mcolors
from rasterio.features import shapes as rio_shapes
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform as rio_transform
from streamlit_folium import st_folium
import folium

REPO_ROOT = Path(__file__).resolve().parent.parent
IA_DIR = REPO_ROOT / "espacializacion" / "output" / "indice_actividad"
MODELO_DIR = REPO_ROOT / "modelo-temporal" / "output"
ESTADO_JSON = REPO_ROOT / "orquestador" / "logs" / "estado_ultima_corrida.json"

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
                f"background:{_hex_con_alpha(color, 0.07)}; color:#6b6b6b; "
                f"font-weight:500; border:2px solid {_hex_con_alpha(color, 0.35)};"
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
        vmax = float(np.nanmax(arr)) if np.any(~np.isnan(arr)) else 1.0
        norm = mcolors.Normalize(vmin=0, vmax=vmax if vmax > 0 else 1.0)
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
        coloraxis_showscale=False, height=420, margin=dict(t=10, b=10, l=10, r=10),
    )
    for i, frame in enumerate(fig.frames):
        frame.name = fechas[i]
    slider = fig.layout.sliders[0]
    nuevos_steps = []
    for i, step in enumerate(slider.steps):
        step_dict = step.to_plotly_json()
        step_dict["label"] = fechas[i]
        step_dict["args"] = [[fechas[i]], step_dict["args"][1]]
        nuevos_steps.append(step_dict)
    slider.steps = nuevos_steps
    slider.currentvalue = dict(prefix="Semana: ")
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
    </style>""",
    unsafe_allow_html=True,
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
            "Localidad", options=list(GID_NOMBRE), format_func=lambda g: GID_NOMBRE[g]
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
            "Semana (fecha de fin)", options=semanas,
            index=semanas.index(st.session_state[ESTADO_SEMANA_KEY]),
            key=f"{ESTADO_SEMANA_KEY}_select", on_change=_semana_desde_selectbox,
        )
    semana = st.session_state[ESTADO_SEMANA_KEY]

    nombre = GID_NOMBRE[gid]

    ia_path = IA_DIR / f"{semana}_{gid}_indice_actividad.tif"
    sigma_path = IA_DIR / f"{semana}_{gid}_sigma.tif"
    arr_ia, bounds = cargar_raster_4326(str(ia_path))
    codigo_activo = codigo_categoria_maxima(gid, arr_ia)

    with col_semaforo:
        st.markdown(semaforo_html(codigo_activo), unsafe_allow_html=True)

    st.subheader(f"{nombre}: {semana}")

    col_mapa, col_ovip = st.columns([3, 2])

    with col_mapa:
        st.markdown("**Índice de actividad**")

        semanas_cronologico = list(reversed(semanas))
        st.select_slider(
            "Recorrer semanas", options=semanas_cronologico,
            value=semana, key=f"{ESTADO_SEMANA_KEY}_slider",
            on_change=_semana_desde_slider, label_visibility="collapsed",
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
            capa_sigma = folium.raster_layers.ImageOverlay(
                image=raster_a_imagen_rgba(arr_sigma, None, cmap_continuo=True),
                bounds=bounds_sigma,
                name="Error (σ, desvío intra-semanal)",
                opacity=0.75,
                show=False,
            )
            capa_sigma.add_to(m)

        folium.LayerControl(collapsed=True).add_to(m)
        m.fit_bounds(bounds)
        st_folium(m, height=460, use_container_width=True, returned_objects=[])

        leyenda = " · ".join(
            f'<span style="color:{c}">■</span> {cat}' for c, cat in zip(PALETA, CATEGORIAS)
        )
        st.markdown(leyenda, unsafe_allow_html=True)
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
                use_container_width=True,
            )
            desde = df_ovip["date"].min().strftime("%Y-%m-%d")
            st.plotly_chart(
                fig_indice_oviposicion(
                    df_ovip, f"Índice de oviposición: desde {desde}",
                    dias_atras=None, height=270,
                ),
                use_container_width=True,
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
                st.plotly_chart(fig_serie, use_container_width=True)
            with col_evol_mapa:
                st.caption(
                    "Arrastrá el control para ver cualquier semana disponible, o tocá "
                    "Play para recorrerlas todas."
                )
                st.plotly_chart(figura_animada_indice_actividad(gid), use_container_width=True)

    with st.expander("Datos meteorológicos"):
        df_met = cargar_serie_meteorologica(gid)
        if df_met is None:
            st.info("Sin datos meteorológicos para esta localidad.")
        else:
            hoy = pd.Timestamp(date.today())
            ventana = df_met[df_met["date"] >= hoy - pd.Timedelta(days=200)]

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
                name="Humedad relativa (%)", line=dict(color="#5aa469", dash="dot"), yaxis="y2",
            ))
            fig_met.add_vline(x=hoy, line_dash="dot", line_color="gray")
            fig_met.update_layout(
                height=350, margin=dict(t=50, b=10, l=10, r=10),
                yaxis=dict(title="Precipitación (mm)"),
                yaxis2=dict(title="°C / %", overlaying="y", side="right"),
                legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0.5, xanchor="center"),
                xaxis=dict(tickformat="%Y-%m-%d"),
            )
            st.plotly_chart(fig_met, use_container_width=True)

with tab_acerca:
    st.header("Acerca de este sistema")
    st.markdown(
        """
Sistema de alerta temprana para *Aedes aegypti*, vector del dengue, en
cuatro localidades de Córdoba (Argentina): Córdoba capital, Río Cuarto,
Villa María y Salsipuedes. Desarrollado en el marco de una tesis doctoral.

**Índice de actividad**: combina dos modelos independientes:

- **Modelo temporal** (dinámica poblacional de Aedes aegypti, modelo de
  Otero/Aguirre): a partir de datos meteorológicos (precipitación IMERG,
  temperatura y humedad GDAS/FNL), estima la dinámica de huevos, larvas,
  pupas y adultos, y de ahí un **índice de oviposición** diario,
  estandarizado (0-1) contra una ventana móvil de 365 días.
- **Modelo espacial** (MCDA: análisis multicriterio): combina NDVI
  (vegetación, Sentinel-2), densidad de construcciones, población y NBI
  (necesidades básicas insatisfechas) en un índice de **idoneidad de
  hábitat** semanal por píxel.

El índice de actividad final es el producto de ambos:
`índice de actividad = idoneidad espacial × índice de oviposición`.

**Clasificación categórica**: los mapas usan 4 categorías (baja, media,
alta, muy alta) calibradas contra datos de ovitrampas de cada localidad,
mediante el umbral de Youden propio de cada una (no una escala global).

**Proyección a futuro**: el índice de oviposición se actualiza
semanalmente e incorpora pronóstico meteorológico (CFS, NOAA) a 14 días.
El índice de actividad espacial, en cambio, depende de imágenes satelitales
reales y por lo tanto es siempre retrospectivo: no se proyecta a futuro.

**Actualización**: el sistema corre automáticamente todos los martes
(ver `orquestador/`), re-descargando clima, re-corriendo el modelo
temporal, incorporando la semana de vegetación más reciente disponible por
satélite, y recalculando el índice de actividad.

**Fuentes de datos**
- Precipitación: NASA GES DISC, GPM IMERG Late (diario, 0.1°)
- Temperatura / humedad: NCEP GDAS/FNL (vía NCAR GDEX)
- Pronóstico: NOAA CFS (vía NOMADS)
- Vegetación: Copernicus Sentinel-2 L2A (NDVI, vía EODAG)
- Población / NBI / construcciones: WorldPop, INDEC, Open Buildings
- Mapas base: Esri World Light Gray Canvas (HERE, Garmin, FAO, NOAA, USGS)
  y Esri World Imagery (Maxar, Earthstar Geographics)
        """
    )
