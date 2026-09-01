FROM debian:bookworm-slim

# python3-rasterio: GDAL del sistema ya resuelto (mismo patron que geoprocesos)
# streamlit/streamlit-folium/plotly: no estan empaquetados en apt, van por pip
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-rasterio \
        python3-pandas \
        python3-numpy \
        python3-matplotlib \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --break-system-packages \
        streamlit \
        streamlit-folium \
        folium \
        plotly

WORKDIR /app
COPY app.py .

# espacializacion/output, modelo-temporal/output y orquestador/logs se leen
# via volumen en tiempo de ejecucion (no van horneados en la imagen).

EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.headless=true"]
