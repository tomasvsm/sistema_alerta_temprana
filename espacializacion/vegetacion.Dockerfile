FROM debian:bookworm-slim

# grass: motor GIS + bindings python (grass.script)
# python3-geopandas: geopandas + GDAL/GEOS/PROJ del sistema ya resueltos
RUN apt-get update && apt-get install -y --no-install-recommends \
        grass \
        python3-geopandas \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --break-system-packages eodag

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
