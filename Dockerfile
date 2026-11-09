FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Exponer el puerto que usará Cloud Run
ENV PORT=8080
EXPOSE 8080
# Usar gunicorn para producción
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"]