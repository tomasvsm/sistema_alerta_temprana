FROM ubuntu:24.04

# grass-core: misma version exacta que el host (8.3.2-1ubuntu2, verificado
# 2026-09-07) -- ver comentario equivalente en vegetacion.Dockerfile.
# gdal-bin: gdalinfo/ogrinfo (variables_MCDA.py los llama via subprocess
# para leer extents de rasters/gpkg).
# python3-rasterio, python3-pandas: usados directo por variables_MCDA.py
# (construcciones se procesa entera con rasterio/numpy/geopandas, sin
# pasar por GRASS).
RUN apt-get update && apt-get install -y --no-install-recommends \
        grass-core \
        gdal-bin \
        python3-geopandas \
        python3-rasterio \
        python3-pandas \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY src/variables_MCDA.py src/
COPY resources/roi/ resources/roi/

# Location GRASS propia (ver comentario en vegetacion.Dockerfile: solo se
# usa si se monta un /grassdata vacio; en produccion se monta encima el
# mapset real del host, que ya la tiene).
RUN grass -c EPSG:5346 /grassdata/posgar2007_4_cba -e

# estaticas/ (salida) la provee el volumen en tiempo de ejecucion -- a
# diferencia de geoprocesos.Dockerfile, esta imagen ES la que genera esos
# archivos, no los consume ya hechos.
RUN mkdir -p estaticas

CMD ["bash"]
