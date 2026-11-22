# Instalación en una VM o servidor Windows

Escenario típico de una empresa: SQL Server en la red con **seguridad integrada de Windows**, una carpeta compartida o de OneDrive/SharePoint como entrada y el equipo de contabilidad revisando desde el navegador.

En Windows lo habitual es la cola `inline` (sin Redis): `invoicebot run` en una tarea programada hace todo el ciclo. Si el volumen crece, apunta `INVOICEBOT_REDIS_URL` a un Redis de la red y ejecuta `invoicebot worker` y `invoicebot scheduler` como servicios (por ejemplo con NSSM o una tarea "al iniciar").

## 1. Requisitos

- [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`): instala Python 3.13 y las dependencias exactas de `uv.lock`. Con Python ya instalado también sirve `py -m venv .venv` y `.venv\Scripts\pip install -e .`.
- [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server).
- Para OCR de escaneos y fotos: [Tesseract de UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) con el idioma español (o `INVOICEBOT_OCR_ENGINE=claude`).

## 2. Instalar

```powershell
git clone https://github.com/ArcGabito/invoice-bot.git C:\InvoiceBot
cd C:\InvoiceBot
uv sync --locked --no-dev      # crea .venv con Python 3.13 y las dependencias de uv.lock
copy .env.example .env
```

## 3. Conectar con SQL Server

Pega en `.env` la cadena de conexión tal como la copias de SSMS o de tu aplicación .NET: InvoiceBot la traduce a ODBC.

```dotenv
INVOICEBOT_DB_CONNECTION_STRING=Data Source=SRV-SQL\PRODUCCION;Integrated Security=True;Encrypt=True;TrustServerCertificate=True
INVOICEBOT_DB_NAME=invoicebot
```

Con seguridad integrada, InvoiceBot se conecta como el usuario de Windows que lo ejecuta. Para la cuenta de servicio de las tareas programadas (recomendado: una gMSA), indícala y deja que `db init` le dé acceso:

```dotenv
INVOICEBOT_DB_WINDOWS_LOGIN=EMPRESA\gmsa-invoicebot$
```

```powershell
.venv\Scripts\invoicebot db init      # con un usuario administrador de la instancia
.venv\Scripts\invoicebot doctor
```

## 4. Programar el ciclo y publicar la pantalla de revisión

```powershell
# PowerShell como administrador
.\scripts\windows\register-scheduled-task.ps1 -ServiceAccount 'EMPRESA\gmsa-invoicebot$' -Serve
```

- `InvoiceBot`: `invoicebot run --trigger scheduled` cada 15 minutos (`-IntervalMinutes`).
- `InvoiceBot-API`: `invoicebot serve`, arranca con Windows y se reinicia si se cae.

Para usarla desde la red, en `.env`: `INVOICEBOT_SERVE_HOST=0.0.0.0` y HTTPS, con un certificado de la empresa (`INVOICEBOT_SERVE_TLS_CERT` / `_KEY`, PEM) o un proxy inverso (IIS con ARR, Caddy) y `INVOICEBOT_FORWARDED_ALLOW_IPS` con la IP del proxy. Abre el puerto en el firewall:

```powershell
New-NetFirewallRule -DisplayName 'InvoiceBot' -Direction Inbound -Protocol TCP -LocalPort 4317 -Action Allow -Profile Domain
```

Crea los usuarios: `.venv\Scripts\invoicebot user add ana --name "Ana Pérez"`.

## 5. Abrir en Visual Studio

`invoice-bot.slnx` abre el proyecto de Python (`invoice-bot.pyproj`) en Visual Studio 2022/2026 con la carga de trabajo *Desarrollo de Python*: F5 inicia la API con el entorno `.venv` y el *Explorador de pruebas* ejecuta pytest.
