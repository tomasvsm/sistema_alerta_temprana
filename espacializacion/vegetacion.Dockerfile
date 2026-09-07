FROM ubuntu:24.04

# grass-core: MISMA version exacta que el host (8.3.2-1ubuntu2, verificado
# 2026-09-07 con `apt-cache policy` en ambos lados) porque viene del mismo
# repo (Ubuntu 24.04 noble, universe) -- adrede ubuntu:24.04 y no
# debian:bookworm-slim, que solo ofrece GRASS 8.2.1 (version distinta,
# riesgo de resultados ligeramente distintos en resampleos/calculos). Es
# el mismo paquete que "grass" (el binario y grass.script son iguales),
# pero es el que el host tiene instalado realmente (`grass` metapackage
# ahi no esta instalado).
# python3-geopandas: geopandas + GDAL/GEOS/PROJ del sistema ya resueltos
RUN apt-get update && apt-get install -y --no-install-recommends \
        grass-core \
        python3-geopandas \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# eodag pide una shapely mas nueva que la de apt (via python3-geopandas), y
# pip no puede pisarla en el mismo site-packages (instalada por dpkg, sin
# RECORD de pip -- con --break-system-packages simple tira error al
# intentar desinstalarla). Un venv con --system-site-packages resuelve
# esto sin ese conflicto: hereda geopandas/numpy de apt (asi no se
# duplica/cambia esa version) y solo agrega/pisa localmente lo que eodag
# necesita (shapely mas nueva, etc.), sin tocar los paquetes de apt.
RUN python3 -m venv --system-site-packages /opt/eodag-venv && \
    /opt/eodag-venv/bin/pip install --no-cache-dir eodag
ENV PATH="/opt/eodag-venv/bin:${PATH}"

WORKDIR /app
COPY src/ src/
COPY resources/roi/ resources/roi/

# Location GRASS propia: POSGAR 2007 / Argentina 4 (EPSG:5346), la misma
# proyeccion que usa el resto del pipeline espacial (Cordoba, faja 4).
RUN grass -c EPSG:5346 /grassdata/posgar2007_4_cba -e

# data/ (NDVI generado) y credenciales EODAG las provee el volumen/entorno
# en tiempo de ejecucion, no van horneadas en la imagen.
RUN mkdir -p data/vegetacion

CMD ["bash"]
