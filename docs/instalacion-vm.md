# Instalación en una máquina virtual (producción)

Guía para TI: InvoiceBot trabaja solo en una VM dedicada y el equipo de Contabilidad revisa desde su PC, en el navegador.

```text
 Empleados ──deja facturas──► \\servidor\Facturas\Entrada ◄──lee cada 15 min── VM InvoiceBot ──HTTPS──► Claude (Anthropic)
                                                                                 │
 Contabilidad ◄──abre el Excel── \\servidor\Contabilidad\InvoiceBot ◄──exporta───┤
 Contabilidad ──revisa en el navegador (HTTPS, usuario y contraseña)────────────►┘ invoicebot serve
```

- **Ciclo del bot** (`invoicebot run`): tarea programada cada 15 minutos.
- **Pantalla de revisión** (`invoicebot serve`): arranca con Windows y se reinicia sola si se cae.
- **IA**: Claude (Anthropic). Ollama es solo para desarrollo: en una VM sin GPU es demasiado lento.
- **Base de datos**: SQLite en la propia VM. Solo el bot y la pantalla de revisión escriben en ella; nadie más la abre.

## 1. Requisitos

| | Mínimo | Recomendado |
| --- | --- | --- |
| Sistema | Windows Server 2022 o Windows 11 | Windows Server 2025 |
| CPU / RAM | 2 vCPU / 4 GB | 4 vCPU / 8 GB (OCR de muchos escaneos) |
| Disco | 20 GB | 40 GB (base de datos, logs y copias locales) |
| GPU | No hace falta | — |
| Software | [Node.js 24 LTS](https://nodejs.org/) y Git (o el .zip de la release) | |

**Salida a internet (HTTPS, puerto 443):**

- `api.anthropic.com`: obligatorio.
- `api-seguridad.sunat.gob.pe` y `api.sunat.gob.pe`: solo si se activa la verificación en SUNAT.

**Entrada desde la red de la oficina:** el puerto de la pantalla de revisión (4317 por defecto).

## 2. Cuenta de servicio

Las tareas deben correr con una cuenta propia, sin privilegios de administrador y sin depender de que alguien inicie sesión:

- **En un dominio:** una cuenta de servicio administrada de grupo (gMSA, p. ej. `EMPRESA\gmsa-invoicebot$`). Windows rota su contraseña solo.
- **Sin dominio:** una cuenta local o de dominio normal (p. ej. `EMPRESA\svc-invoicebot`) con contraseña que no caduque.

Permisos que necesita:

| Recurso | Permiso |
| --- | --- |
| Derecho "Iniciar sesión como proceso por lotes" (directiva de seguridad local o GPO) | Concedido |
| Carpeta del proyecto (`C:\InvoiceBot`): `data`, `logs`, `exports` | Modificar |
| Carpeta de entrada (`\\servidor\Facturas\Entrada`) | Lectura |
| Carpeta de exportaciones y de copias en red | Modificar |

> Usa rutas UNC (`\\servidor\recurso`), no unidades de red (`Z:`). Las unidades mapeadas no existen cuando la tarea corre sin sesión iniciada.

## 3. Instalación

En PowerShell como administrador:

```powershell
git clone https://github.com/ArcGabito/invoice-bot.git C:\InvoiceBot
cd C:\InvoiceBot
git checkout v0.4.0
npm ci --omit=dev
copy .env.example .env
```

## 4. Configuración de producción (`.env`)

```ini
# IA: Claude
INVOICEBOT_AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Carpetas
INVOICEBOT_INBOX_DIR=\\servidor\Facturas\Entrada
INVOICEBOT_EXPORT_DIR=\\servidor\Contabilidad\InvoiceBot
INVOICEBOT_RUN_EXPORT=xlsx
INVOICEBOT_BACKUP_DIR=\\servidor\Respaldos\InvoiceBot
INVOICEBOT_LOG_DIR=./logs

# OCR: tesseract (local, gratis) o claude (mejor con fotos difíciles; la imagen va a Anthropic)
INVOICEBOT_OCR_ENGINE=tesseract

# Pantalla de revisión en red
INVOICEBOT_SERVE_HOST=0.0.0.0
INVOICEBOT_SERVE_PORT=4317
INVOICEBOT_SERVE_TLS_PFX=./data/tls/revision.pfx
INVOICEBOT_SERVE_TLS_PASSPHRASE=...
INVOICEBOT_SERVE_SESSION_HOURS=10
```

`.env` contiene la clave de Anthropic y la contraseña del certificado. Deja que solo lo lean la cuenta de servicio y los administradores:

```powershell
icacls C:\InvoiceBot\.env /inheritance:r /grant:r 'EMPRESA\gmsa-invoicebot$:R' '*S-1-5-32-544:F'
```

(`*S-1-5-32-544` es el grupo Administradores en cualquier idioma de Windows.) Haz lo mismo con la carpeta `data\tls`, que guarda el certificado.

## 5. Certificado HTTPS

La pantalla de revisión no se abre a la red sin HTTPS: las contraseñas y los datos de las facturas no deben viajar sin cifrar.

- **Con una autoridad certificadora interna (recomendado):** pide un certificado de servidor para el nombre de la VM (p. ej. `invoicebot.empresa.local`) en formato `.pfx`. Cópialo a `C:\InvoiceBot\data\tls\revision.pfx` y pon su contraseña en `INVOICEBOT_SERVE_TLS_PASSPHRASE`. Los navegadores lo aceptan sin avisos.
- **Sin autoridad interna:** crea uno autofirmado:

  ```powershell
  .\scripts\windows\new-review-certificate.ps1 -DnsName 'invoicebot.empresa.local', 'invoicebot'
  ```

  El script muestra las líneas que hay que copiar a `.env`. Distribuye `data\tls\revision.cer` a los PC de Contabilidad por GPO, en *Entidades de certificación raíz de confianza*. Si no, el navegador mostrará una advertencia.

También se admite un par PEM (`INVOICEBOT_SERVE_TLS_CERT` y `INVOICEBOT_SERVE_TLS_KEY`). Si la empresa ya publica sus aplicaciones internas detrás de un proxy HTTPS (IIS, nginx…), puedes dejar que el proxy haga el HTTPS y poner `INVOICEBOT_SERVE_ALLOW_HTTP=true`, siempre que el puerto 4317 solo sea accesible desde el proxy.

## 6. Usuarios de la pantalla de revisión

Cada persona que revisa tiene su usuario. Su nombre queda en la auditoría de cada aprobación, rechazo o corrección.

```powershell
node bin\invoicebot.js user add ana.perez --name "Ana Pérez"
node bin\invoicebot.js user list
node bin\invoicebot.js user reset-password ana.perez
node bin\invoicebot.js user disable ana.perez
```

- `user add` y `reset-password` generan una contraseña aleatoria y la muestran una sola vez. Entrégala por un canal seguro.
- Para fijar una contraseña concreta desde un script, usa `--password-stdin`. Debe tener al menos 12 caracteres.
- `disable` impide el acceso al instante y cierra las sesiones abiertas de esa persona; su historial de revisiones se conserva. Úsalo cuando alguien deja la empresa.
- Tras 5 intentos fallidos, el usuario y la IP quedan bloqueados 15 minutos. Los accesos y los rechazos quedan en el log.

## 7. Firewall

Abre el puerto solo para la red de la oficina:

```powershell
New-NetFirewallRule -DisplayName 'InvoiceBot - revisión' -Direction Inbound -Protocol TCP -LocalPort 4317 `
  -RemoteAddress 192.168.10.0/24 -Profile Domain -Action Allow
```

## 8. Tareas programadas

En PowerShell como administrador:

```powershell
.\scripts\windows\register-scheduled-task.ps1 -ServiceAccount 'EMPRESA\gmsa-invoicebot$' -Serve -IntervalMinutes 15
```

Esto registra dos tareas:

- **`InvoiceBot`** ejecuta `invoicebot run` cada 15 minutos.
- **`InvoiceBot-Revision`** ejecuta `invoicebot serve` al arrancar Windows y se reinicia si se cae.

Con una cuenta normal, el script pide su contraseña una vez. Usa `-WhatIf` para ver lo que haría sin cambiar nada y `-Unregister` para quitar las dos tareas.

## 9. Comprobación

```powershell
node bin\invoicebot.js doctor          # Node, base de datos, OCR, Claude, certificado y usuarios
Start-ScheduledTask -TaskName InvoiceBot
node bin\invoicebot.js status          # resultado de la última ejecución
```

Desde un PC de Contabilidad, abre `https://invoicebot.empresa.local:4317/` y entra con un usuario.

## 10. Operación

| Tarea | Cómo |
| --- | --- |
| Ver qué hizo el bot | `invoicebot status`, o los logs diarios en `logs\` (JSON) |
| Copias de seguridad | Automáticas cada 24 h en `INVOICEBOT_BACKUP_DIR`, verificadas; `invoicebot backup --list` |
| Supervisión | `invoicebot status --json` desde la herramienta de monitorización; la tabla `bot_runs` guarda cada ciclo |
| Reinicio de la VM | Las dos tareas vuelven solas. Las sesiones de la pantalla se cierran y cada persona vuelve a entrar |
| Cambio de clave de Anthropic | Edita `.env`. El ciclo siguiente ya la usa; para la pantalla, reinicia la tarea `InvoiceBot-Revision` |
| Renovar el certificado | Reemplaza el `.pfx` y reinicia la tarea `InvoiceBot-Revision` |

**Actualizar a una versión nueva:**

```powershell
Disable-ScheduledTask InvoiceBot; Stop-ScheduledTask InvoiceBot-Revision
cd C:\InvoiceBot; git fetch --tags; git checkout v0.X.0; npm ci --omit=dev
node bin\invoicebot.js doctor          # aplica las migraciones; antes guarda una copia de la base de datos
Enable-ScheduledTask InvoiceBot; Start-ScheduledTask InvoiceBot-Revision
```

## Limitaciones conocidas

- **Una sola VM.** La base de datos es SQLite local: no se puede repartir el trabajo entre varias máquinas.
- **Usuarios propios.** Todavía no hay inicio de sesión con Active Directory ni Microsoft Entra ID.
- **Contraseñas.** Cada persona no puede cambiarse la contraseña a sí misma: la cambia TI con `reset-password`.
- **Sesiones en memoria.** Reiniciar la pantalla de revisión obliga a entrar de nuevo.
