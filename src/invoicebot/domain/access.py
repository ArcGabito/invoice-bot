"""Roles, permisos y empresas: quién puede hacer qué y sobre qué comprobantes.

| Rol | Para quién | Puede |
| --- | --- | --- |
| ``admin`` | Responsable del sistema | Todo, incluidos usuarios, claves y empresas |
| ``accountant`` | Asistente o analista contable | Subir, revisar, corregir, exportar, SUNAT, tipo de cambio, ciclos |
| ``approver`` | Jefe de área o gerente | Ver y aprobar o rechazar pagos (flujo de aprobación) |
| ``treasury`` | Tesorería | Ver y exportar lo aprobado |
| ``auditor`` | Auditoría interna o externa | Solo lectura, incluido el historial de cambios |

Las claves de API tienen alcance ``read`` o ``write`` (como en la 1.0) o una lista de
permisos finos, y pueden limitarse a una empresa.
"""

from __future__ import annotations

from collections.abc import Iterable

INVOICES_READ = "invoices:read"
DOCUMENTS_WRITE = "documents:write"
REVIEW_WRITE = "review:write"
APPROVALS_WRITE = "approvals:write"
PURCHASING_WRITE = "purchasing:write"
EXPORTS_READ = "exports:read"
FX_WRITE = "fx:write"
SUNAT_WRITE = "sunat:write"
RUNS_WRITE = "runs:write"
JOBS_READ = "jobs:read"
JOBS_WRITE = "jobs:write"
AUDIT_READ = "audit:read"
ADMIN = "admin"

PERMISSIONS: frozenset[str] = frozenset(
    {
        INVOICES_READ,
        DOCUMENTS_WRITE,
        REVIEW_WRITE,
        APPROVALS_WRITE,
        PURCHASING_WRITE,
        EXPORTS_READ,
        FX_WRITE,
        SUNAT_WRITE,
        RUNS_WRITE,
        JOBS_READ,
        JOBS_WRITE,
        AUDIT_READ,
        ADMIN,
    }
)

ROLES: dict[str, frozenset[str]] = {
    "admin": PERMISSIONS,
    "accountant": frozenset(
        {
            INVOICES_READ,
            DOCUMENTS_WRITE,
            REVIEW_WRITE,
            PURCHASING_WRITE,
            EXPORTS_READ,
            FX_WRITE,
            SUNAT_WRITE,
            RUNS_WRITE,
            JOBS_READ,
            JOBS_WRITE,
            AUDIT_READ,
        }
    ),
    "approver": frozenset({INVOICES_READ, APPROVALS_WRITE, EXPORTS_READ}),
    "treasury": frozenset({INVOICES_READ, EXPORTS_READ}),
    "auditor": frozenset({INVOICES_READ, EXPORTS_READ, JOBS_READ, AUDIT_READ}),
}

ROLE_LABELS = {
    "admin": "Administrador",
    "accountant": "Contabilidad",
    "approver": "Aprobador",
    "treasury": "Tesorería",
    "auditor": "Auditoría",
}

#: Alcances de las claves de API de la 1.0.
API_SCOPES: dict[str, frozenset[str]] = {
    "read": frozenset({INVOICES_READ, EXPORTS_READ, JOBS_READ}),
    "write": frozenset(
        {
            INVOICES_READ,
            EXPORTS_READ,
            JOBS_READ,
            DOCUMENTS_WRITE,
            REVIEW_WRITE,
            PURCHASING_WRITE,
            FX_WRITE,
            SUNAT_WRITE,
            RUNS_WRITE,
            JOBS_WRITE,
        }
    ),
}


class AccessError(ValueError):
    pass


def role_permissions(role: str) -> frozenset[str]:
    try:
        return ROLES[role]
    except KeyError as error:
        raise AccessError(f"Rol desconocido {role!r} (usa {', '.join(ROLES)})") from error


def parse_permissions(raw: str | Iterable[str] | None) -> frozenset[str]:
    """ "invoices:read, documents:write" → conjunto validado (ADMIN no se da a claves)."""
    if raw is None:
        return frozenset()
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    wanted = frozenset(p.strip() for p in items if p and p.strip())
    unknown = wanted - PERMISSIONS
    if unknown:
        raise AccessError(f"Permisos desconocidos: {', '.join(sorted(unknown))}")
    if ADMIN in wanted:
        raise AccessError("Una clave de API no puede tener el permiso admin")
    return wanted


def api_key_permissions(scope: str, permissions: str | None) -> frozenset[str]:
    if permissions:
        return parse_permissions(permissions)
    return API_SCOPES.get(scope, API_SCOPES["read"])
