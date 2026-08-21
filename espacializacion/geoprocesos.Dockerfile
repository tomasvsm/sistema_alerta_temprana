FROM debian:bookworm-slim

# python3-rasterio ya resuelve GDAL del sistema de forma consistente
# (mismo patron que uso con python3-geopandas en el Dockerfile de vegetacion)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-rasterio \
        python3-pandas \
        python3-numpy \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY src/calculo_mcda.py src/calculo_indice_actividad.py src/

# estaticas/ (capas 100m, ~6MB) SI se hornean en la imagen: son estables,
# no cambian semana a semana como vegetacion/modelo-temporal.
COPY estaticas/ estaticas/

# data/vegetacion (entrada), output/ (salida) y el CSV de oviposicion de
# modelo-temporal se leen via volumen en tiempo de ejecucion.
RUN mkdir -p data/vegetacion output/MCDA output/indice_actividad

CMD ["bash"]
