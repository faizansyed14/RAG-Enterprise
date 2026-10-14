FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Exact versions come from requirements.lock (a `pip freeze` of the tested image);
# requirements.txt only names what the app needs. Test tooling is not installed.
COPY requirements.txt requirements-scale.txt requirements-connectors.txt requirements-docparse.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock

# Enterprise layer: hybrid index, workers, connectors. Optional extras are build args (see docker-compose files):
#   INSTALL_CONNECTORS=1  (default on) packages for the S3 / SMB / SFTP / SQL connectors (default on)
#   INSTALL_DOCPARSE=1    (default on) local OCR + layout + table models for scanned documents (adds ~400 MB; two-step install,
#                         see requirements-docparse.txt)
#   INSTALL_LEGACY_OFFICE=1  (default on) LibreOffice, to read .doc .xls .ppt .ods .odp (adds ~450 MB)
ARG INSTALL_CONNECTORS=1
ARG INSTALL_DOCPARSE=1
ARG INSTALL_LEGACY_OFFICE=1
RUN if [ "$INSTALL_LEGACY_OFFICE" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui fonts-dejavu-core \
      && rm -rf /var/lib/apt/lists/*; \
    fi
RUN pip install --no-cache-dir -r requirements-scale.txt -c requirements.lock \
    && if [ "$INSTALL_CONNECTORS" = "1" ]; then pip install --no-cache-dir -r requirements-connectors.txt -c requirements.lock; fi \
    && if [ "$INSTALL_DOCPARSE" = "1" ]; then \
         pip install --no-cache-dir -r requirements-docparse.txt -c requirements.lock \
         && pip install --no-cache-dir --no-deps rapidocr-onnxruntime==1.4.4 rapid-layout==1.2.1 rapid-table==1.0.3; \
       fi

# Run as an unprivileged user; the tree engine's data dir must be writable by it.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/.rag-data \
    && chown -R app:app /app
COPY --chown=app:app . .
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=6s --start-period=40s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5).status == 200 else 1)"

# Migrations run before the server starts (single-instance deployments; run
# `alembic upgrade head` as a separate job instead if you scale out).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-server-header"]
