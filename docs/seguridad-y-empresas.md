# Empresas, roles, inicio de sesión único y auditoría

## Multiempresa

Un estudio contable o un grupo empresarial puede llevar varias empresas en una sola instalación. Cada empresa es un RUC adquirente:

```bash
invoicebot company add 20601234565 "Comercial Andina S.A.C." --retention-agent
invoicebot company add 20100070970 "Inversiones del Sur S.A."
invoicebot company list
```

- Cada comprobante se asigna a la empresa cuyo RUC figura como adquirente (XML o PDF). Al registrar una empresa, se le asignan los comprobantes que ya estaban guardados.
- Si hay empresas registradas y un comprobante está dirigido a otro RUC, queda con la advertencia `CUSTOMER_NOT_A_COMPANY` (suele ser un comprobante enviado por error o de otra razón social del grupo).
- Sin empresas registradas, InvoiceBot funciona como en la 1.0 (una sola empresa).

### Aislamiento entre empresas

Una persona o una clave de API puede limitarse a ciertas empresas. El filtro no depende de que cada consulta lo recuerde: lo aplica **SQL Server con Row-Level Security** (política `invoicebot_company_policy` sobre `invoices`). La API fija en cada conexión `SESSION_CONTEXT('invoicebot.companies')` con las empresas de quien llama; los workers, la CLI y el programador no lo fijan y ven todo.

```bash
invoicebot user add ana --name "Ana Pérez" --role accountant --company 20601234565
invoicebot user companies ana --company 20601234565 --company 20100070970
invoicebot user companies ana --all
invoicebot apikey create erp-andina --scope write --company 20601234565
```

## Roles

| Rol | Para quién | Permisos |
| --- | --- | --- |
| `admin` | Responsable del sistema | Todo, incluidos usuarios, claves y empresas |
| `accountant` | Contabilidad | Subir, revisar, corregir, exportar, SUNAT, tipo de cambio, ciclos, trabajos, historial |
| `approver` | Jefe de área o gerente | Ver, exportar y aprobar pagos |
| `treasury` | Tesorería | Ver y exportar |
| `auditor` | Auditoría interna o externa | Solo lectura, trabajos e historial |

Los usuarios que existían antes de la migración 007 quedan como `admin`. Para cambiar un rol: `invoicebot user set-role ana approver`.

Las claves de API conservan los alcances `read` y `write`, o pueden tener permisos finos:

```bash
invoicebot apikey create escaner --permissions documents:write
invoicebot apikey create power-bi --permissions invoices:read,exports:read
```

Permisos disponibles: `invoices:read`, `documents:write`, `review:write`, `approvals:write`, `purchasing:write`, `exports:read`, `fx:write`, `sunat:write`, `runs:write`, `jobs:read`, `jobs:write` y `audit:read` (`admin` no se da a claves).

## Inicio de sesión con Microsoft o Google (OIDC)

La pantalla de ingreso muestra **Entrar con Microsoft** (o Google) cuando se configura un proveedor OpenID Connect. Se usa el flujo de código con PKCE; el `id_token` se verifica con las claves públicas del proveedor (firma, emisor, audiencia, vigencia y nonce).

### Microsoft Entra ID

1. Entra ID → *App registrations* → *New registration*. URI de redirección (Web): `https://facturas.empresa.pe/auth/oidc/callback`.
2. *Certificates & secrets* → nuevo secreto de cliente.
3. En `.env`:

```dotenv
INVOICEBOT_OIDC_ISSUER=https://login.microsoftonline.com/<id-del-directorio>/v2.0
INVOICEBOT_OIDC_CLIENT_ID=<id-de-la-aplicación>
INVOICEBOT_OIDC_CLIENT_SECRET=<secreto>
INVOICEBOT_OIDC_ALLOWED_DOMAINS=empresa.pe
```

Usa el ID del directorio, no `common`: así solo entran cuentas de tu organización.

### Google Workspace

Google Cloud → *APIs & Services* → *Credentials* → *OAuth client ID* (aplicación web) con la misma URI de redirección.

```dotenv
INVOICEBOT_OIDC_ISSUER=https://accounts.google.com
INVOICEBOT_OIDC_CLIENT_ID=<id>.apps.googleusercontent.com
INVOICEBOT_OIDC_CLIENT_SECRET=<secreto>
INVOICEBOT_OIDC_ALLOWED_DOMAINS=empresa.pe
```

### Quién puede entrar

1. Quien ya entró antes (se reconoce por su identificador en el proveedor).
2. Un usuario creado por un administrador con el mismo correo (`invoicebot user add ana --email ana@empresa.pe` o `invoicebot user email ana ana@empresa.pe`): en su primer ingreso se vincula.
3. Si `INVOICEBOT_OIDC_AUTO_CREATE_ROLE` está definido (`accountant`, `approver`, `treasury` o `auditor`, nunca `admin`), cualquier persona de los dominios permitidos se da de alta sola con ese rol.

El MFA y las políticas de acceso condicional las pone el proveedor. Los usuarios locales con contraseña siguen funcionando (útil para una cuenta de emergencia).

## Auditoría

- **Facturas**: un trigger guarda cada alta, cambio y baja en `invoice_changes`, con el estado resultante y quién lo hizo: la persona (`ana`), la clave de API (`api:erp`) o el trabajo del worker (`job:documents.process#123`).
- **Empresas**: igual, en `company_changes`.
- **Revisión**: `review_actions` (aprobar, corregir, rechazar…) como en la 1.0.

```bash
invoicebot history 123
curl -H "Authorization: Bearer $KEY" https://facturas.empresa.pe/api/v1/invoices/123/history
```

> No se usan tablas temporales de SQL Server: con `READ_COMMITTED_SNAPSHOT` y conexiones reutilizadas, una modificación concurrente puede fallar con el error 13535. El trigger no tiene ese problema.
