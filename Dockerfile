# syntax=docker/dockerfile:1.7
# InvoiceBot: una sola imagen para todos los procesos. Cambia solo el comando:
#   serve       API REST y pantalla de revisión   (Container App / servicio `api`)
#   worker      colas de Redis (Dramatiq)          (Container App con KEDA / servicio `worker`)
#   scheduler   tareas programadas                 (servicio `scheduler`)
#   schedule tick · db migrate · run               (Container Apps Jobs)
# Ver docs/despliegue-azure.md y docs/despliegue-vps.md.

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.13.9

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --- Compilación: dependencias con uv, exactamente las de uv.lock ------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential unixodbc-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# Primero solo las dependencias: esta capa se reutiliza mientras no cambie uv.lock.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY pyproject.toml uv.lock README.md LICENSE.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- Imagen final (sin uv ni compiladores) --------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/ArcGabito/invoice-bot" \
      org.opencontainers.image.description="InvoiceBot: RPA de comprobantes peruanos (FastAPI + SQL Server + Redis)" \
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
# Solo tiene sentido para `serve`: en Compose, worker y scheduler la desactivan.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)" || exit 1

ENTRYPOINT ["invoicebot"]
CMD ["serve"]
