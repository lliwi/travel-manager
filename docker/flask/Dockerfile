FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime dependencies. Tesseract carries the Spanish and English language
# packs because OCR runs against Spanish booking documents; poppler provides
# the PDF rasteriser that OCR needs.
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
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY docker/flask/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

# Non-root, with only the directories it actually writes to.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/logs /app/data \
    && chown -R appuser:appuser /app/logs /app/data

EXPOSE 5000
USER appuser

ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "wsgi:app"]
