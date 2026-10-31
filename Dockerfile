# InvoiceBot en contenedor: el ciclo del bot (`run --watch`) y la pantalla de
# revisión (`serve`) usan esta misma imagen. Ver docker-compose.yml y
# docs/instalacion-linux.md.

# --- Compilación: TypeScript a dist/ ---
FROM node:24-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

# --- Dependencias de producción ---
FROM node:24-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# Ningún paquete de producción necesita scripts de instalación: @napi-rs/canvas
# trae su binario precompilado como dependencia opcional.
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force

# --- Imagen final ---
FROM node:24-bookworm-slim
ENV NODE_ENV=production \
    INVOICEBOT_DATA_DIR=/app/data \
    INVOICEBOT_EXPORT_DIR=/app/exports \
    INVOICEBOT_LOG_DIR=/app/logs
WORKDIR /app

# Mismo UID que el usuario de SQL Server en su imagen oficial (10001, grupo 0):
# así el bot puede borrar las copias antiguas que SQL Server escribe en el
# volumen compartido de copias (retención).
RUN useradd --uid 10001 --gid 0 --no-create-home --shell /usr/sbin/nologin invoicebot \
 && mkdir -p /app/data /app/exports /app/logs \
 && chown -R 10001:0 /app/data /app/exports /app/logs \
 && chmod -R g=u /app/data /app/exports /app/logs

COPY --from=deps /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY package.json LICENSE.md ./

USER 10001:0
VOLUME ["/app/data"]
EXPOSE 4317

ENTRYPOINT ["node", "dist/cli/bin.js"]
CMD ["run", "--watch"]
