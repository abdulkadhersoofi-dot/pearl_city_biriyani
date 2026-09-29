# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

# WeasyPrint's native dependencies for PDF generation (invoices, POS
# receipts). Package names verified against a working install, not
# guessed - a missing one here fails silently deep inside WeasyPrint.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libharfbuzz0b \
    libfontconfig1 \
    fonts-liberation \
    fonts-dejavu-core \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x deploy/entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    FLASK_APP=wsgi.py \
    FLASK_ENV=production

EXPOSE 8000

CMD ["deploy/entrypoint.sh"]
