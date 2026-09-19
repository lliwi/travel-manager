FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# The worker does the heavy lifting: OCR, PDF rasterising and antivirus
# streaming all happen here rather than in the web container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        postgresql-client \
        libmagic1 \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-spa \
        tesseract-ocr-eng \
        curl \
        redis-tools \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY docker/celery/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

RUN useradd --create-home --uid 1000 celeryuser \
    && mkdir -p /app/logs /app/data \
    && chown -R celeryuser:celeryuser /app/logs /app/data

USER celeryuser

ENTRYPOINT ["/entrypoint.sh"]
CMD ["celery", "-A", "app.tasks.celery_app.celery", "worker", "--loglevel=info"]
