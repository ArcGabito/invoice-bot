# Conectar el buzón de facturas de la empresa (guía para TI)

InvoiceBot lee un buzón compartido, por ejemplo `facturas@empresa.pe`, y procesa los comprobantes que llegan adjuntos. Esta guía explica cómo darle acceso **solo a ese buzón**, sin usuario ni contraseña de una persona, en Microsoft 365 y en Google Workspace.

Qué hace InvoiceBot con el buzón:

- Lista los correos con adjuntos de los últimos días (`INVOICEBOT_MAIL_SINCE_DAYS`, 7 por defecto) y descarga cada uno una sola vez.
- Guarda los XML, PDF, fotos y el contenido de los ZIP en la carpeta de entrada.
- Opcionalmente ordena el buzón: mueve los correos con comprobantes a una carpeta (`INVOICEBOT_MAIL_PROCESSED_FOLDER`, p. ej. `Procesado`) y los que no traían comprobantes a otra (`INVOICEBOT_MAIL_REVIEW_FOLDER`, p. ej. `Revisar`). Si las carpetas no existen, las crea.
- **Nunca borra mensajes ni envía correos.**

Sin carpetas de destino basta con permiso de lectura. Para ordenar el buzón hace falta permiso de lectura y escritura.

## Microsoft 365

### 1. Registrar la aplicación

En el [centro de administración de Microsoft Entra](https://entra.microsoft.com): **Aplicaciones → Registros de aplicaciones → Nuevo registro**.

- Nombre: `InvoiceBot`.
- Tipos de cuenta: **Solo las cuentas de este directorio organizativo**.
- Sin URI de redirección.

Anota el **Id. de aplicación (cliente)** y el **Id. de directorio (inquilino)**.

### 2. Credencial: certificado (recomendado) o secreto

**Certificado.** Genera uno en el servidor de InvoiceBot:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 730 \
  -subj "/CN=InvoiceBot" -keyout invoicebot.key -out invoicebot.crt
cat invoicebot.crt invoicebot.key > invoicebot.pem   # certificado + clave, para InvoiceBot
chmod 600 invoicebot.pem && rm invoicebot.key
```

Sube **solo** `invoicebot.crt` (la parte pública) en **Certificados y secretos → Certificados → Cargar certificado**.

Si TI entrega un `.pfx`, conviértelo: `openssl pkcs12 -in invoicebot.pfx -out invoicebot.pem -nodes` (con OpenSSL 3 y archivos antiguos puede hacer falta `-legacy`). Si prefieres dejar la clave cifrada, omite `-nodes` y pon la contraseña en `INVOICEBOT_MAIL_MS_CERT_PASSPHRASE`.

**Secreto.** En **Certificados y secretos → Secretos de cliente → Nuevo secreto de cliente**. Copia el _valor_ (no el id). Vence: anota la fecha para renovarlo.

### 3. Permiso limitado al buzón de facturas

Un permiso de aplicación `Mail.Read` otorgado en Entra permite leer **todos** los buzones de la empresa. Para limitarlo a uno, usa **RBAC para aplicaciones de Exchange Online** y **no** otorgues `Mail.Read` en Entra (los permisos se suman: si lo otorgas en Entra, la restricción no sirve).

En PowerShell, con el módulo ExchangeOnlineManagement:

```powershell
Connect-ExchangeOnline

# Id. de aplicación (cliente) y Id. de objeto de la aplicación empresarial
# (Entra → Aplicaciones empresariales → InvoiceBot → Id. de objeto).
New-ServicePrincipal -AppId <id-aplicacion> -ObjectId <id-objeto-app-empresarial> -DisplayName "InvoiceBot"

New-ManagementScope -Name "InvoiceBot - buzón de facturas" `
  -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'facturas@empresa.pe'"

# Solo lectura:
New-ManagementRoleAssignment -App <id-aplicacion> -Role "Application Mail.Read" `
  -CustomResourceScope "InvoiceBot - buzón de facturas"
# O, para ordenar el buzón (mover a Procesado / Revisar):
# New-ManagementRoleAssignment -App <id-aplicacion> -Role "Application Mail.ReadWrite" `
#   -CustomResourceScope "InvoiceBot - buzón de facturas"

# Comprobación: debe decir InScope = True para el buzón de facturas y False para otro.
Test-ServicePrincipalAuthorization -Identity <id-aplicacion> -Resource facturas@empresa.pe
Test-ServicePrincipalAuthorization -Identity <id-aplicacion> -Resource gerencia@empresa.pe
```

Los cambios pueden tardar en aplicarse (Microsoft indica hasta dos horas).

### 4. Configurar InvoiceBot

```ini
INVOICEBOT_MAIL_PROVIDER=graph
INVOICEBOT_MAIL_MS_TENANT=<id-directorio>
INVOICEBOT_MAIL_MS_CLIENT_ID=<id-aplicacion>
INVOICEBOT_MAIL_MS_CERT_FILE=/ruta/segura/invoicebot.pem
# o, con secreto: INVOICEBOT_MAIL_MS_CLIENT_SECRET=<valor>
INVOICEBOT_MAIL_USER=facturas@empresa.pe
# Opcional, exige Application Mail.ReadWrite:
INVOICEBOT_MAIL_PROCESSED_FOLDER=Procesado
INVOICEBOT_MAIL_REVIEW_FOLDER=Revisar
```

Prueba con `invoicebot mail check`. Un error 403 significa que falta el permiso o que el buzón está fuera del ámbito.

## Google Workspace

Hay dos formas. Elige según lo que permita la política de la empresa.

### Opción A: inicio de sesión con la cuenta del buzón (más restringida)

El acceso queda limitado a la cuenta con la que se inicia sesión.

1. En [Google Cloud](https://console.cloud.google.com), crea un proyecto y activa la **Gmail API**.
2. **Pantalla de consentimiento de OAuth**: tipo de usuario **Interno**. Así no requiere verificación de Google y el acceso no vence a los 7 días (eso solo pasa con apps _Externas_ en modo de prueba).
3. **Credenciales → Crear credenciales → ID de cliente de OAuth → App de escritorio**.
4. En InvoiceBot:

   ```ini
   INVOICEBOT_MAIL_PROVIDER=gmail
   INVOICEBOT_MAIL_GOOGLE_CLIENT_ID=<id-cliente>
   INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET=<secreto-cliente>
   ```

5. Ejecuta `invoicebot mail login` e inicia sesión con `facturas@empresa.pe`. El acceso se guarda en `data/mail-token.json`; protégelo como una contraseña.

Si después configuras carpetas de destino, vuelve a ejecutar `mail login`: el permiso cambia de `gmail.readonly` a `gmail.modify`.

### Opción B: cuenta de servicio con delegación de todo el dominio (sin usuario)

**Atención**: la delegación de dominio permite a la cuenta de servicio actuar como **cualquier usuario** del dominio con los permisos autorizados. Google no permite limitarla a un buzón. Protege la clave JSON y úsala solo si la política de la empresa lo acepta.

1. En Google Cloud, en el proyecto con la Gmail API activada: **IAM y administración → Cuentas de servicio → Crear**. En **Claves → Agregar clave → JSON** descarga la clave (si la organización bloquea la creación de claves con la política `iam.disableServiceAccountKeyCreation`, usa la opción A).
2. Anota el **ID único** (numérico) de la cuenta de servicio.
3. En la [consola de administración](https://admin.google.com): **Seguridad → Acceso y control de datos → Controles de API → Delegación de todo el dominio → Agregar**:
   - ID de cliente: el ID único del paso 2.
   - Permisos: `https://www.googleapis.com/auth/gmail.readonly`, o `https://www.googleapis.com/auth/gmail.modify` para ordenar el buzón.
4. En InvoiceBot:

   ```ini
   INVOICEBOT_MAIL_PROVIDER=gmail
   INVOICEBOT_MAIL_GOOGLE_SERVICE_ACCOUNT_FILE=/ruta/segura/cuenta-servicio.json
   INVOICEBOT_MAIL_USER=facturas@empresa.pe
   INVOICEBOT_MAIL_PROCESSED_FOLDER=Procesado   # opcional (etiqueta)
   INVOICEBOT_MAIL_REVIEW_FOLDER=Revisar        # opcional (etiqueta)
   ```

En Gmail, "mover" significa poner la etiqueta y quitar el correo de la bandeja de entrada. Con carpetas de destino configuradas, InvoiceBot lee solo la bandeja de entrada.

## Correo de hosting o Gmail personal (IMAP)

Para cPanel, Zimbra, Zoho o Gmail con contraseña de aplicación, usa `INVOICEBOT_MAIL_PROVIDER=imap` (ver el README). Con carpetas de destino, la carpeta se abre con escritura y los mensajes se mueven con el comando `MOVE` del servidor.

## Trazabilidad y operación

- Cada factura recuerda de qué correo vino (remitente, asunto, fecha y buzón). Se ve con `invoicebot show <factura>` y en la pantalla de revisión.
- `invoicebot mail list` muestra los últimos correos leídos, qué se hizo con cada uno y a qué carpeta se movió.
- Si mover un mensaje falla (por ejemplo, falta el permiso de escritura), el mensaje queda registrado igual y se intenta moverlo en el ciclo siguiente, sin descargarlo otra vez.
- Si las credenciales son rechazadas, el ciclo continúa con la carpeta de entrada, queda como **parcial** y el error aparece en `invoicebot status` y en los logs.
- Renovaciones: el certificado o el secreto de Microsoft vencen en la fecha que elegiste al crearlos. InvoiceBot rechaza un certificado vencido con un mensaje claro.
