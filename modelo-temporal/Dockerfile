FROM python:3.8-slim

# --- dependencias de sistema ---
# build-essential: compilar el motor C++ (otero_precipitation_wrapper)
# libeigen3-dev: headers de Eigen (algebra lineal usada por el motor C++)
# libeccodes-dev: libreria nativa que necesita pygrib para leer los GRIB2 de GDAS
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libeigen3-dev \
        libeccodes-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# pygrib==2.1.6 usa sintaxis Cython vieja (estilo Python 2) que no compila
# con el Cython moderno que pip trae por default en un build aislado (PEP517).
# Se instala aparte, con --no-build-isolation, para que use el Cython<3 y el
# numpy ya instalados en vez de bajar los ultimos.
RUN pip install --no-cache-dir "cython<3" numpy==1.24.4 \
    && pip install --no-cache-dir --no-build-isolation pygrib==2.1.6 \
    && pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY resources/*.cfg resources/
COPY resources/*.template resources/

# --- compilar el motor C++ (pybind11 + Eigen) ---
# Sin -march=native: la imagen se compila una vez y puede correr en
# cualquier CPU, no solo en la que hizo el build.
RUN g++ -std=c++17 -Wall -O3 -shared -fPIC \
        $(python3 -m pybind11 --includes) \
        -I/usr/include/eigen3 \
        src/cpp/otero_precipitation_wrapper.cpp \
        -o src/otero_precipitation_wrapper.so

# data/ y logs/ los provee el volumen en tiempo de ejecucion (no van en la imagen)
RUN mkdir -p data/public logs

# resources/passwords.cfg NO se copia (esta en .dockerignore / gitignore) --
# se monta como volumen o se inyecta por variables de entorno en runtime.

CMD ["python3", "--version"]
