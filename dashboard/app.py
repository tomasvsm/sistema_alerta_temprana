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
import plotly.graph_objects as go
import rasterio
import streamlit as st
from matplotlib import colors as mcolors
from rasterio.vrt import WarpedVRT
from streamlit_folium import st_folium
import folium

REPO_ROOT = Path(__file__).resolve().parent.parent
IA_DIR = REPO_ROOT / "espacializacion" / "output" / "indice_actividad"
MODELO_DIR = REPO_ROOT / "modelo-temporal" / "output"
ESTADO_JSON = REPO_ROOT / "orquestador" / "logs" / "estado_ultima_corrida.json"

GID_NOMBRE = {
    "1252": "Villa María",
    "1271": "Salsipuedes",
    "1300": "Río Cuarto",
    "1385": "Córdoba",
}
GID_SNAKE = {
    "1252": "villa_maria",
    "1271": "salsipuedes",
    "1300": "rio_cuarto",
    "1385": "cordoba",
}
# Umbral de Youden propio de cada localidad, calibrado contra ovitrampas
# reales (validacion/cruce/analisis_correlacion.ipynb). Mismo criterio que
# espacializacion/generar_pdf_mapas.py -- no es una escala arbitraria.
YOUDEN = {"1252": 0.1544, "1271": 0.0829, "1300": 0.3736, "1385": 0.2360}
PALETA = ["#2b83ba", "#83c1ab", "#e0f3b5", "#d7191c"]
CATEGORIAS = ["Actividad baja", "Actividad media", "Actividad alta", "Actividad muy alta"]


def bounds_categoricos(gid: str) -> list[float]:
    youden = YOUDEN[gid]
    tercio = (1.0 - youden) / 3
    return [0.0, youden, youden + tercio, youden + 2 * tercio, 1.0]


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


def cargar_estado_orquestador() -> dict | None:
    if not ESTADO_JSON.exists():
        return None
    with open(ESTADO_JSON) as f:
        return json.load(f)


# --------------------------------------------------------------------------
st.set_page_config(page_title="Alerta temprana Aedes aegypti", layout="wide")

estado = cargar_estado_orquestador()
if estado and estado.get("hubo_error"):
    pasos_error = [p for p, s in estado["pasos"].items() if str(s).startswith("ERROR")]
    st.error(
        f"⚠️ La última corrida semanal ({estado['fecha_ref']}) tuvo errores en: "
        f"**{', '.join(pasos_error)}**. Los datos mostrados pueden no estar actualizados "
        f"en esas etapas. Log: `{estado['log']}`"
    )

st.title("Sistema de alerta temprana — *Aedes aegypti*")
st.caption("Córdoba, Argentina — Córdoba capital · Río Cuarto · Villa María · Salsipuedes")

tab_panel, tab_acerca = st.tabs(["Panel", "Acerca de"])

with tab_panel:
    with st.sidebar:
        st.header("Selección")
        gid = st.selectbox(
            "Localidad", options=list(GID_NOMBRE), format_func=lambda g: GID_NOMBRE[g]
        )
        semanas = semanas_disponibles(gid)
        if not semanas:
            st.warning("No hay semanas procesadas para esta localidad todavía.")
            st.stop()
        semana = st.selectbox("Semana (fecha de fin)", options=semanas, index=0)

    nombre = GID_NOMBRE[gid]
    st.subheader(f"{nombre} — semana finalizada el {semana}")

    col_mapa, col_ovip = st.columns([3, 2])

    with col_mapa:
        ia_path = IA_DIR / f"{semana}_{gid}_indice_actividad.tif"
        sigma_path = IA_DIR / f"{semana}_{gid}_sigma.tif"

        arr_ia, bounds = cargar_raster_4326(str(ia_path))
        centro = [(bounds[0][0] + bounds[1][0]) / 2, (bounds[0][1] + bounds[1][1]) / 2]

        m = folium.Map(location=centro, zoom_start=12, tiles="OpenStreetMap")
        folium.raster_layers.ImageOverlay(
            image=raster_a_imagen_rgba(arr_ia, gid, cmap_continuo=False),
            bounds=bounds,
            name="Índice de actividad",
            opacity=0.75,
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

        folium.LayerControl(collapsed=False).add_to(m)
        st_folium(m, height=420, use_container_width=True, returned_objects=[])

        leyenda = " · ".join(
            f'<span style="color:{c}">■</span> {cat}' for c, cat in zip(PALETA, CATEGORIAS)
        )
        st.markdown(leyenda, unsafe_allow_html=True)
        st.caption(
            f"Umbral de Youden de esta localidad: {YOUDEN[gid]:.4f} "
            "(calibrado contra ovitrampas reales)."
        )

    with col_ovip:
        df_ovip = cargar_indice_oviposicion(gid)
        if df_ovip is None:
            st.info("Sin datos de índice de oviposición para esta localidad.")
        else:
            hoy = pd.Timestamp(date.today())
            real = df_ovip[df_ovip["date"] <= hoy]
            pronost = df_ovip[df_ovip["date"] >= hoy]

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=real["date"], y=real["indice_oviposicion"],
                mode="lines", name="Confirmado", line=dict(color="#1f77b4"),
            ))
            if not pronost.empty:
                fig.add_trace(go.Scatter(
                    x=pronost["date"], y=pronost["indice_oviposicion"],
                    mode="lines", name="Pronosticado (CFS, 14 días)",
                    line=dict(color="#1f77b4", dash="dash"),
                ))
            fig.add_vline(x=hoy, line_dash="dot", line_color="gray")
            fig.update_layout(
                title="Índice de oviposición",
                yaxis_range=[0, 1], yaxis_title="Índice (0-1)",
                xaxis_title=None, height=440, margin=dict(t=80, b=10, l=10, r=10),
                legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0.5, xanchor="center"),
                xaxis_range=[hoy - pd.Timedelta(days=180), hoy + pd.Timedelta(days=20)],
            )
            st.plotly_chart(fig, use_container_width=True)

    with st.expander("Serie meteorológica cruda (precipitación, temperatura, humedad)"):
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
            )
            st.plotly_chart(fig_met, use_container_width=True)

with tab_acerca:
    st.header("Acerca de este sistema")
    st.markdown(
        """
Sistema de alerta temprana para *Aedes aegypti*, vector del dengue, en
cuatro localidades de Córdoba (Argentina): Córdoba capital, Río Cuarto,
Villa María y Salsipuedes. Desarrollado en el marco de una tesis doctoral.

**Índice de actividad** — combina dos modelos independientes:

- **Modelo temporal** (dinámica poblacional de Aedes aegypti, modelo de
  Otero/Aguirre): a partir de datos meteorológicos (precipitación IMERG,
  temperatura y humedad GDAS/FNL), estima la dinámica de huevos, larvas,
  pupas y adultos, y de ahí un **índice de oviposición** diario,
  estandarizado (0-1) contra una ventana móvil de 365 días.
- **Modelo espacial** (MCDA — análisis multicriterio): combina NDVI
  (vegetación, Sentinel-2), densidad de construcciones, población y NBI
  (necesidades básicas insatisfechas) en un índice de **idoneidad de
  hábitat** semanal por píxel.

El índice de actividad final es el producto de ambos:
`índice de actividad = idoneidad espacial × índice de oviposición`.

**Clasificación categórica** — los mapas usan 4 categorías (baja, media,
alta, muy alta) calibradas contra ovitrampas reales de cada localidad,
mediante el umbral de Youden propio de cada una (no una escala global).

**Proyección a futuro** — el índice de oviposición se actualiza
semanalmente e incorpora pronóstico meteorológico (CFS, NOAA) a 14 días.
El índice de actividad espacial, en cambio, depende de imágenes satelitales
reales y por lo tanto es siempre retrospectivo: no se proyecta a futuro.

**Actualización** — el sistema corre automáticamente todos los martes
(ver `orquestador/`), re-descargando clima, re-corriendo el modelo
temporal, incorporando la semana de vegetación más reciente disponible por
satélite, y recalculando el índice de actividad.

**Fuentes de datos**
- Precipitación: NASA GES DISC, GPM IMERG Late (diario, 0.1°)
- Temperatura / humedad: NCEP GDAS/FNL (vía NCAR GDEX)
- Pronóstico: NOAA CFS (vía NOMADS)
- Vegetación: Copernicus Sentinel-2 L2A (NDVI, vía EODAG)
- Población / NBI / construcciones: WorldPop, INDEC, Open Buildings
        """
    )
