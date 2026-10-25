FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000 DB_PATH=/data/watch.db
VOLUME /data
CMD ["sh", "-c", "gunicorn app:app -w 1 --threads 8 --timeout 120 --bind 0.0.0.0:${PORT}"]
