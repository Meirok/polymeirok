FROM python:3.11-slim

# Directorio de trabajo dentro del contenedor
WORKDIR /app

# Copiar solo requirements primero para aprovechar el caché de capas de Docker
COPY requirements.txt .

# Instalar dependencias sin caché para reducir tamaño de imagen
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código fuente
COPY . .

# Crear directorio de logs
RUN mkdir -p /app/logs

# Variables de entorno por defecto (serán sobreescritas por .env o docker-compose)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DRY_RUN=true \
    PRODUCTION=false

# Puerto no necesario (bot no expone HTTP), pero documentado para referencia
# EXPOSE 8080

# Comando por defecto: iniciar el bot en modo simulación
CMD ["python", "main.py"]
