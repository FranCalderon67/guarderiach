FROM python:3.11-slim
 
WORKDIR /app
 
# Instalamos dependencias del sistema necesarias para compilar o procesar PDFs si hiciera falta
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
&& rm -rf /var/lib/apt/lists/*
 
# Copiamos el archivo de requerimientos de Python
COPY requirements*.txt ./
 
# Instalamos los paquetes de pip
RUN pip install --no-cache-dir -r requirements*.txt
 
# Copiamos todo el código fuente de Flask
COPY . .
 
# Creamos la carpeta interna donde la app espera guardar datos
RUN mkdir -p /capitalhumano
 
# Exponemos el puerto de producción
EXPOSE 8000
 
# Arrancamos con Gunicorn (estándar de producción para Flask)
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]