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
#
# Bug real encontrado 2026-09-08 (rompio "clima" en produccion desde al
# menos el 2026-09-01, silencioso porque el resto de la cadena sigue
# igual): pygrib usa layout "src/" (codigo en src/pygrib/, no pygrib/) y
# necesita setuptools>=61 para que su [tool.setuptools.packages.find] lo
# descubra. Con --no-build-isolation pip usa el setuptools YA instalado
# en la imagen base (57.5 en python:3.8-slim), que no entiende ese
# layout: compila bien la extension C (_pygrib.so) pero nunca copia
# src/pygrib/__init__.py -- el resultado es un "pygrib" que importa sin
# error pero esta vacio (paquete de namespace implicito), y revienta mas
# tarde con "module 'pygrib' has no attribute 'open'". Ademas pygrib
# necesita pyproj en runtime (esta en sus dependencies de pyproject.toml)
# y no estaba instalado -- quedaba tapado por el bug de arriba, nunca se
# llegaba a importar tan profundo. Verificado con un GRIB2 real
# (gdas1.fnl0p25.2023091618.f03.grib2) despues del fix.
RUN pip install --no-cache-dir "cython<3" numpy==1.24.4 "setuptools>=61" pyproj \
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
