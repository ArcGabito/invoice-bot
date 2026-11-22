# Despliegue en un VPS (Docker Compose)

Para un VPS Linux (DigitalOcean, Hetzner, Contabo, un servidor de la oficina…) con Docker y Docker Compose v2. También sirve con Dokploy o Coolify, que leen el mismo `docker-compose.yml`.

| Servicio | Qué hace | Perfil |
| --- | --- | --- |
| `api` | API REST y pantalla de revisión en `127.0.0.1:8000` | siempre |
| `worker` | Colas `ingest`, `sunat` y `default`: correo, carpeta de entrada, SUNAT, tipo de cambio, exportaciones, copias | siempre |
| `extractor` | Cola `extract`: lectura de cada documento (XML, PDF, OCR, IA). Más CPU y memoria | siempre |
| `scheduler` | Tareas programadas (`INVOICEBOT_SCHEDULE_*`) y reintentos vencidos | siempre |
| `redis` | Colas, límites de llamadas compartidos y caché (AOF, sin desalojo) | siempre |
| `migrate` | Aplica las migraciones y termina | `tools` (`docker compose run --rm migrate`) |
| `sqlserver` | SQL Server 2022 Express en el mismo VPS | `sql` |
| `caddy` | HTTPS automático (Let's Encrypt) delante de la API | `proxy` |

```text
 correo · carpeta · API ─► api / scheduler ──encolan──► redis ──► worker (ingest, sunat, default)
                                                          └─────► extractor (extract) ─► SQL Server
```

## Instalación

```bash
git clone https://github.com/ArcGabito/invoice-bot.git && cd invoice-bot
cp .env.example .env
nano .env     # ver abajo
mkdir -p inbox exports && sudo chown -R 10001:0 inbox exports
```

Valores mínimos en `.env`:

```dotenv
INVOICEBOT_AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
# Redis del propio compose (la URL la arma docker-compose.yml)
REDIS_PASSWORD=una-clave-larga-y-aleatoria
# SQL Server en el mismo VPS (perfil sql)
MSSQL_SA_PASSWORD=Una-Clave-Larga-2026!
INVOICEBOT_DB_AUTH=sql
INVOICEBOT_DB_USER=invoicebot_app
INVOICEBOT_DB_PASSWORD=Otra-Clave-Larga-2026!
INVOICEBOT_DB_TRUST_CERT=true          # certificado autofirmado del contenedor, red interna de Docker
INVOICEBOT_DB_ADMIN_USER=sa            # solo para `db init`; bórralo después
INVOICEBOT_DB_ADMIN_PASSWORD=Una-Clave-Larga-2026!
# HTTPS (perfil proxy)
INVOICEBOT_DOMAIN=facturas.empresa.pe
INVOICEBOT_SERVE_COOKIE_SECURE=true
# Excel del mes cada mañana
INVOICEBOT_RUN_EXPORT=xlsx
```

Arranque:

```bash
docker compose --profile sql up -d sqlserver redis
docker compose run --rm migrate db init
docker compose run --rm api user add admin --name "Administrador"   # anota la contraseña
docker compose run --rm api apikey create email-bot --scope write  # si otro sistema enviará documentos
docker compose --profile sql --profile proxy up -d
docker compose logs -f worker extractor scheduler
```

Abre `https://facturas.empresa.pe/`. El DNS del dominio debe apuntar al VPS y los puertos 80 y 443 deben estar abiertos. Redis y SQL Server no publican puertos fuera del VPS.

## Escalar

Cada réplica de `extractor` procesa `INVOICEBOT_WORKER_THREADS` documentos a la vez; las réplicas se reparten la cola sin duplicar trabajo.

```bash
docker compose up -d --scale extractor=3 --scale worker=2
```

Para no exceder el plan de la IA o el límite de SUNAT entre todas las réplicas, usa `INVOICEBOT_AI_RATE_PER_MINUTE` e `INVOICEBOT_SUNAT_RATE_PER_MINUTE` (el contador está en Redis). Con varias réplicas de la API no hace falta nada: no guarda estado.

## Trabajos y programación

```bash
docker compose exec api invoicebot schedule list        # programaciones y próxima ejecución
docker compose exec api invoicebot jobs list --status failed
docker compose exec api invoicebot jobs show 123
docker compose exec api invoicebot jobs retry 123
docker compose exec api invoicebot enqueue sunat.verify --payload '{"month": "2026-10"}'
```

Cada trabajo queda en la tabla `jobs` de SQL Server (intentos, resultado, error). Si Redis se reinicia, el programador vuelve a publicar los trabajos que quedaron sin enviar. Las métricas para Prometheus están en `GET /metrics` (con una clave de API de lectura).

## Con un SQL Server o Azure SQL existente

Omite el perfil `sql` y define `INVOICEBOT_DB_SERVER`, `INVOICEBOT_DB_PORT`, `INVOICEBOT_DB_USER` e `INVOICEBOT_DB_PASSWORD` (y `INVOICEBOT_DB_TRUST_CERT=false` si el servidor tiene un certificado válido, como Azure SQL).

## Carpeta de entrada

`./inbox` del VPS se monta en `/data/inbox`. Los comprobantes pueden llegar por:

- el **correo** (`INVOICEBOT_MAIL_PROVIDER`), que se lee con la tarea `inbox.collect`;
- la **API** (`POST /api/v1/documents`), desde email-bot, el ERP o un escáner: responde `202` con el trabajo (`GET /api/v1/jobs/{id}`);
- copia directa (SFTP, `rclone` desde OneDrive/SharePoint/Google Drive, Syncthing).

## Actualizar

```bash
git pull
docker compose pull            # o: docker compose build
docker compose run --rm migrate
docker compose --profile sql --profile proxy up -d
```

`migrate` hace una copia de seguridad antes de aplicar migraciones (volumen `sqlserver-backups`). Los workers reciben SIGTERM y terminan el trabajo en curso (hasta 2 minutos) antes de reiniciarse.

## Copias de seguridad

La tarea `db.backup` hace `BACKUP DATABASE … WITH COPY_ONLY, CHECKSUM` según `INVOICEBOT_SCHEDULE_BACKUP` y conserva `INVOICEBOT_BACKUP_RETENTION`. Llévalas fuera del VPS:

```bash
docker run --rm -v invoice-bot_sqlserver-backups:/b -v "$PWD":/out busybox sh -c 'cp /b/*.bak /out/'
```

Redis guarda sus datos en el volumen `redis-data` (AOF), pero no hace falta respaldarlo: la fuente de verdad de cada trabajo es SQL Server.

## Sin Redis

Para una sola máquina con poco volumen puedes no usar colas: `INVOICEBOT_QUEUE_BACKEND=inline` y un único servicio con `command: ['run', '--watch']`, como en la versión 1.0.
