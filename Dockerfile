# InvoiceBot: una sola imagen para la API (Container App / servicio `api`) y para el
# ciclo RPA (Container Apps Job / servicio `worker`). Ver docs/despliegue-azure.md y
# docs/despliegue-vps.md.

ARG PYTHON_VERSION=3.13

# --- Compilación: dependencias en un entorno virtual ------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential unixodbc-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY pyproject.toml README.md LICENSE.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# --- Imagen final -------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm
ARG TARGETARCH
LABEL org.opencontainers.image.source="https://github.com/ArcGabito/invoice-bot" \
      org.opencontainers.image.description="InvoiceBot: RPA de comprobantes peruanos (FastAPI + SQL Server)" \
      org.opencontainers.image.licenses="MIT"

# Controlador ODBC 18 de Microsoft (SQL Server y Azure SQL) y Tesseract con español.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb -o /tmp/mprod.deb \
 && dpkg -i /tmp/mprod.deb && rm /tmp/mprod.deb \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
      tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng tzdata \
 && apt-get purge -y curl gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Lima \
    INVOICEBOT_DATA_DIR=/data/app \
    INVOICEBOT_INBOX_DIR=/data/inbox \
    INVOICEBOT_EXPORT_DIR=/data/exports \
    INVOICEBOT_UPLOAD_DIR=/data/inbox/api \
    INVOICEBOT_SERVE_HOST=0.0.0.0 \
    INVOICEBOT_SERVE_PORT=8000 \
    INVOICEBOT_FORWARDED_ALLOW_IPS=* \
    INVOICEBOT_LOG_FORMAT=json \
    INVOICEBOT_LOG_LEVEL=info

COPY --from=build /opt/venv /opt/venv

# Mismo UID que el usuario de SQL Server en su imagen oficial (10001, grupo 0): así,
# en Docker Compose, el bot puede borrar las copias antiguas del volumen compartido.
RUN useradd --uid 10001 --gid 0 --no-create-home --shell /usr/sbin/nologin invoicebot \
 && mkdir -p /data/app /data/inbox /data/exports \
 && chown -R 10001:0 /data && chmod -R g=u /data

USER 10001:0
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)" || exit 1

ENTRYPOINT ["invoicebot"]
CMD ["serve"]
