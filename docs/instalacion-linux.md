# Instalación en un servidor Linux con Docker (producción)

Guía para TI: InvoiceBot y SQL Server corren en contenedores en una VM Linux, y el equipo de Contabilidad revisa desde su PC, en el navegador. Para un servidor Windows sin Docker, usa [instalacion-vm.md](instalacion-vm.md).

```text
                         ┌──────────────────────── VM Linux (Docker Compose) ────────────────────────┐
 Empleados ──facturas──► │ /srv/facturas ──► bot (invoicebot run --watch) ──► sqlserver (SQL Server) │ ──HTTPS──► Claude
                         │                   review (invoicebot serve) ◄──┘        │                  │
                         │ /srv/invoicebot/exports ◄── Excel               copias ─┘ volumen          │
                         └───────────────────────────────────────────────────────────────────────────┘
 Contabilidad ──https://invoicebot.empresa.local:4317 (usuario y contraseña)──► review
```

| Servicio       | Qué hace                                                                                                                   |
| -------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `sqlserver`    | SQL Server 2022 (Express por defecto). Datos y copias en volúmenes de Docker. Solo escucha en `127.0.0.1` del anfitrión.   |
| `bot`          | `invoicebot run --watch`: cada 15 minutos reintenta, escanea la carpeta de entrada, verifica en SUNAT, exporta y respalda. |
| `review`       | `invoicebot serve`: pantalla de revisión en red, con usuarios y HTTPS.                                                     |
| `init-volumes` | Prepara los permisos de la carpeta de copias al arrancar y termina.                                                        |

Los tres servicios se reinician solos si se caen y al reiniciar la VM (`restart: unless-stopped`).

## 1. Requisitos

|           | Mínimo                                                       | Recomendado                          |
| --------- | ------------------------------------------------------------ | ------------------------------------ |
| Sistema   | Ubuntu 22.04, Debian 12, RHEL 9 o similar, x86-64            | Ubuntu 24.04 LTS                     |
| CPU / RAM | 2 vCPU / 4 GB (SQL Server necesita 2 GB)                     | 4 vCPU / 8 GB                        |
| Disco     | 30 GB                                                        | 60 GB (base de datos, copias y logs) |
| Software  | Docker Engine 24 o posterior con el plugin Compose v2, y Git |                                      |

SQL Server en Linux solo funciona en x86-64: no en ARM (Raspberry Pi, Graviton, Apple Silicon sin emulación).

**Salida a internet (HTTPS, 443):** `api.anthropic.com` (obligatorio); `mcr.microsoft.com`, `registry-1.docker.io` y `registry.npmjs.org` para construir la imagen; `api-seguridad.sunat.gob.pe` y `api.sunat.gob.pe` solo si se activa la verificación en SUNAT.

**Entrada desde la red de la oficina:** el puerto de la pantalla de revisión (4317).

**Licencia de SQL Server:** la edición Express es gratuita en producción hasta 10 GB por base de datos, de sobra para cientos de miles de facturas. Developer **no** se puede usar en producción. Con licencia propia, pon `MSSQL_PID=Standard` o `Enterprise`.

## 2. Instalación

```bash
sudo git clone https://github.com/ArcGabito/invoice-bot.git /srv/invoicebot
cd /srv/invoicebot
sudo git checkout v0.5.0
sudo cp .env.example .env
sudo chmod 600 .env
```

Los contenedores de InvoiceBot corren con el UID 10001, el mismo que SQL Server en su imagen oficial. Prepara las carpetas que se montan desde el anfitrión:

```bash
sudo mkdir -p exports logs tls
sudo chown 10001:0 exports logs
sudo chmod 770 exports logs
```

La carpeta de entrada puede ser local o un recurso de red montado (SMB/CIFS o NFS) donde los empleados dejan las facturas. El bot solo necesita **leerla** (se monta en modo lectura):

```bash
# Ejemplo con un recurso compartido de Windows, en /etc/fstab:
# //servidor/Facturas/Entrada  /srv/facturas  cifs  credentials=/root/.smb-invoicebot,ro,uid=10001,gid=0,file_mode=0440,dir_mode=0550  0  0
```

## 3. Configuración (`.env`)

Docker Compose lee el mismo `.env`: sus valores llegan a los contenedores y también rellenan `docker-compose.yml`. Las rutas dentro de los contenedores (`INVOICEBOT_INBOX_DIR`, `_EXPORT_DIR`, `_BACKUP_DIR`, `INVOICEBOT_DB_SERVER`…) las fija `docker-compose.yml`: no hace falta tocarlas.

```ini
# SQL Server en el contenedor
MSSQL_SA_PASSWORD=...              # 8+ caracteres con mayúsculas, minúsculas, números y símbolos
MSSQL_PID=Express

# Inicio de sesión de InvoiceBot (lo crea `db init`)
INVOICEBOT_DB_NAME=invoicebot
INVOICEBOT_DB_USER=invoicebot_app
INVOICEBOT_DB_PASSWORD=...         # distinta de la de sa

# IA: Claude
INVOICEBOT_AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Carpetas del anfitrión
INVOICEBOT_INBOX_HOST_DIR=/srv/facturas
INVOICEBOT_EXPORT_HOST_DIR=./exports
INVOICEBOT_RUN_EXPORT=xlsx

# Pantalla de revisión (HTTPS obligatorio en red; el certificado va en ./tls)
INVOICEBOT_SERVE_TLS_PFX=/app/tls/revision.pfx
INVOICEBOT_SERVE_TLS_PASSPHRASE=...
INVOICEBOT_SERVE_PUBLISH=4317
```

Genera contraseñas fuertes con `openssl rand -base64 24`. Los contenedores del bot y de la pantalla no reciben `MSSQL_SA_PASSWORD`.

## 4. Certificado HTTPS

La pantalla escucha en la red, así que no arranca sin HTTPS. Pide a la CA interna de la empresa un certificado para el nombre de la VM (p. ej. `invoicebot.empresa.local`) en `.pfx`, o un par PEM (`INVOICEBOT_SERVE_TLS_CERT=/app/tls/cert.pem` e `INVOICEBOT_SERVE_TLS_KEY=/app/tls/key.pem`). Cópialo a `./tls` y deja que solo lo lea el contenedor:

```bash
sudo cp revision.pfx tls/
sudo chown -R 10001:0 tls && sudo chmod 750 tls && sudo chmod 640 tls/*
```

Sin CA interna, crea uno autofirmado (y distribuye `revision.crt` a los PC de Contabilidad como raíz de confianza):

```bash
sudo openssl req -x509 -newkey rsa:3072 -sha256 -days 825 -nodes \
  -keyout tls/key.pem -out tls/revision.crt \
  -subj "/CN=invoicebot.empresa.local" \
  -addext "subjectAltName=DNS:invoicebot.empresa.local,DNS:invoicebot"
sudo cp tls/revision.crt tls/cert.pem
```

Si la empresa ya publica sus aplicaciones detrás de un proxy HTTPS (nginx, Traefik…), puedes dejar el HTTPS al proxy: `INVOICEBOT_SERVE_ALLOW_HTTP=true` y `INVOICEBOT_SERVE_PUBLISH=127.0.0.1:4317`, para que solo el proxy llegue al puerto.

## 5. Primer arranque

```bash
sudo docker compose build
sudo docker compose up -d sqlserver          # espera a que quede "healthy": docker compose ps
```

Crea la base de datos y el inicio de sesión de InvoiceBot con la cuenta `sa` (la contraseña se pide por teclado y no queda en el historial):

```bash
read -rsp 'Contraseña de sa: ' SA_PASSWORD; echo
sudo docker compose run --rm \
  -e INVOICEBOT_DB_ADMIN_USER=sa -e INVOICEBOT_DB_ADMIN_PASSWORD="$SA_PASSWORD" \
  bot db init
unset SA_PASSWORD
```

Crea los usuarios de la pantalla de revisión (cada uno firma sus revisiones):

```bash
sudo docker compose run --rm bot user add ana.perez --name "Ana Pérez"
```

La contraseña aleatoria se muestra una sola vez: entrégala por un canal seguro. Luego arranca todo:

```bash
sudo docker compose up -d
sudo docker compose run --rm bot doctor
```

Desde un PC de Contabilidad, abre `https://invoicebot.empresa.local:4317/`.

## 6. Firewall

Abre el puerto de la pantalla solo a la red de la oficina. Docker publica los puertos por delante de `ufw`, así que filtra en la cadena `DOCKER-USER`, o publica en una IP interna concreta (`INVOICEBOT_SERVE_PUBLISH=192.168.10.20:4317`):

```bash
sudo iptables -I DOCKER-USER -p tcp --dport 4317 ! -s 192.168.10.0/24 -j DROP
```

SQL Server solo escucha en `127.0.0.1` del anfitrión: no es accesible desde la red.

## 7. Operación

Todos los comandos de la CLI se ejecutan con `docker compose run --rm bot <comando>`:

| Tarea                         | Cómo                                                                                                                                           |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Ver qué hizo el bot           | `docker compose run --rm bot status`; `docker compose logs -f bot`; logs diarios en `./logs`                                                   |
| Supervisión                   | `docker compose run --rm bot status --json`; la tabla `bot_runs` guarda cada ciclo                                                             |
| Usuarios                      | `docker compose run --rm bot user list` · `reset-password <usuario>` · `disable <usuario>`                                                     |
| Copias de seguridad           | Automáticas cada 24 h en el volumen `sqlserver-backups`, verificadas, se conservan las 14 últimas. `docker compose run --rm bot backup --list` |
| Sacar una copia del servidor  | `docker compose cp sqlserver:/var/opt/mssql/backup/<archivo>.bak .` y llévala fuera de la VM                                                   |
| Cambiar la clave de Anthropic | Edita `.env` y `docker compose up -d` (recrea los contenedores con el valor nuevo)                                                             |
| Renovar el certificado        | Reemplaza el archivo en `./tls` y `docker compose restart review`                                                                              |
| Reinicio de la VM             | Los servicios vuelven solos. Las sesiones de la pantalla se cierran y cada persona vuelve a entrar                                             |

**Las copias no salen de la VM por sí solas**: el volumen `sqlserver-backups` está en el mismo disco que la base de datos. Programa en el anfitrión una copia de esa carpeta (`docker volume inspect invoicebot_sqlserver-backups` muestra su ruta) a otro servidor o a almacenamiento externo.

**Restaurar una copia:**

```bash
sudo docker compose stop bot review
sudo docker compose exec sqlserver /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -Q \
  "RESTORE DATABASE invoicebot FROM DISK = N'/var/opt/mssql/backup/invoicebot-AAAAMMDD-HHMMSS.bak' WITH REPLACE"
sudo docker compose start bot review
```

(`sqlcmd` pide la contraseña de sa.) Tras restaurar, el inicio de sesión `invoicebot_app` sigue funcionando porque la base conserva su usuario.

**Actualizar a una versión nueva:**

```bash
cd /srv/invoicebot
sudo git fetch --tags && sudo git checkout v0.X.0
sudo docker compose build
sudo docker compose run --rm bot db migrate   # antes de migrar guarda una copia de la base de datos
sudo docker compose up -d
```

## Limitaciones conocidas

- **SQL Server en el mismo servidor.** Para usar el SQL Server de la empresa, quita el servicio `sqlserver` (y su dependencia) de `docker-compose.yml` y define `INVOICEBOT_DB_SERVER`, `INVOICEBOT_DB_TRUST_CERT=false` e `INVOICEBOT_BACKUP_DIR` en `environment`. La retención de copias solo funciona si el bot ve la carpeta de copias en la misma ruta que SQL Server.
- **Usuarios propios.** Todavía no hay inicio de sesión con Active Directory ni Microsoft Entra ID.
- **Sesiones en memoria.** Reiniciar el servicio `review` obliga a entrar de nuevo.
