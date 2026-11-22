"""Reglas puras de cuentas por pagar: match con la orden de compra y matriz de aprobación.

Sin base de datos: reciben datos simples y devuelven decisiones explicables, para que
cada excepción tenga un motivo legible y las pruebas no necesiten SQL Server.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

# --- Match -------------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchTolerance:
    #: Diferencia admitida en porcentaje del importe de la orden (1.0 = 1 %).
    percent: float = 1.0
    #: Y como mínimo este importe en céntimos (p. ej. 1000 = S/ 10 por redondeos).
    cents: int = 1000

    def allowed(self, base_cents: int) -> int:
        return max(self.cents, round(abs(base_cents) * self.percent / 100))


@dataclass(frozen=True)
class MatchException:
    code: str
    message: str
    expected: str | None = None
    actual: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass(frozen=True)
class OrderFacts:
    """Lo que se sabe de la orden de compra al momento del match."""

    po_number: str
    supplier_ruc: str
    currency: str
    status: str
    requires_receipt: bool
    #: Importe de la orden sin IGV.
    ordered_cents: int
    #: Valor recibido sin IGV (notas de ingreso, guías, conformidades).
    received_cents: int
    #: Ya facturado por otras facturas de la misma orden (sin esta).
    invoiced_cents: int


@dataclass(frozen=True)
class InvoiceFacts:
    supplier_ruc: str | None
    currency: str | None
    #: Importe sin IGV.
    net_cents: int


@dataclass
class MatchOutcome:
    #: matched | exception
    status: str
    match_type: str
    exceptions: list[MatchException] = field(default_factory=list)


def match_invoice(
    invoice: InvoiceFacts, order: OrderFacts, tolerance: MatchTolerance
) -> MatchOutcome:
    """Match a 2 vías (factura ↔ orden) o a 3 vías (factura ↔ orden ↔ recepción)."""
    three_way = order.requires_receipt
    problems: list[MatchException] = []
    if order.status != "open":
        problems.append(
            MatchException(
                "PO_NOT_OPEN",
                f"La orden {order.po_number} está {order.status}",
                "open",
                order.status,
            )
        )
    if invoice.supplier_ruc != order.supplier_ruc:
        problems.append(
            MatchException(
                "PO_SUPPLIER_MISMATCH",
                "El proveedor de la factura no es el de la orden de compra",
                order.supplier_ruc,
                invoice.supplier_ruc,
            )
        )
    if (invoice.currency or "PEN") != order.currency:
        problems.append(
            MatchException(
                "PO_CURRENCY_MISMATCH",
                "La moneda de la factura no es la de la orden de compra",
                order.currency,
                invoice.currency,
            )
        )
    pending_order = order.ordered_cents - order.invoiced_cents
    if invoice.net_cents > pending_order + tolerance.allowed(order.ordered_cents):
        problems.append(
            MatchException(
                "PO_AMOUNT_EXCEEDED",
                "La factura supera lo que queda por facturar de la orden",
                _money(pending_order),
                _money(invoice.net_cents),
            )
        )
    if three_way:
        if order.received_cents <= 0:
            problems.append(
                MatchException(
                    "PO_NOT_RECEIVED",
                    "No hay recepción (nota de ingreso, guía o conformidad) para la orden",
                )
            )
        else:
            pending_received = order.received_cents - order.invoiced_cents
            if invoice.net_cents > pending_received + tolerance.allowed(order.received_cents):
                problems.append(
                    MatchException(
                        "PO_RECEIPT_EXCEEDED",
                        "La factura supera el valor recibido pendiente de facturar",
                        _money(pending_received),
                        _money(invoice.net_cents),
                    )
                )
    return MatchOutcome(
        "exception" if problems else "matched",
        "three_way" if three_way else "two_way",
        problems,
    )


def _money(cents: int) -> str:
    return f"{cents / 100:,.2f}"


# --- Aprobaciones --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    id: int
    level: int
    approver: str | None
    company_id: int | None = None
    cost_center: str | None = None
    supplier_ruc: str | None = None
    min_cents: int = 0
    max_cents: int | None = None
    sla_hours: int = 48
    escalate_to: str | None = None

    @property
    def specificity(self) -> int:
        return (
            (4 if self.supplier_ruc else 0)
            + (2 if self.cost_center else 0)
            + (1 if self.company_id is not None else 0)
        )

    def applies(
        self,
        *,
        company_id: int | None,
        cost_center: str | None,
        supplier_ruc: str | None,
        amount: int,
    ) -> bool:
        return (
            (self.company_id is None or self.company_id == company_id)
            and (self.cost_center is None or self.cost_center == cost_center)
            and (self.supplier_ruc is None or self.supplier_ruc == supplier_ruc)
            and self.min_cents <= amount
            and (self.max_cents is None or amount < self.max_cents)
        )


@dataclass(frozen=True)
class Step:
    level: int
    #: None: el nivel se aprueba solo (sin persona).
    approver: str | None
    rule_id: int
    sla_hours: int
    escalate_to: str | None


def approval_steps(
    rules: Iterable[Rule],
    *,
    company_id: int | None,
    cost_center: str | None,
    supplier_ruc: str | None,
    amount_pen_cents: int,
) -> list[Step]:
    """Niveles de aprobación que necesita una factura, en orden.

    Por cada nivel gana la regla más específica (proveedor > centro de costo > empresa >
    general); a igual especificidad, la más antigua. Sin reglas aplicables, la factura
    no necesita aprobación.
    """
    best: dict[int, Rule] = {}
    for rule in rules:
        if not rule.applies(
            company_id=company_id,
            cost_center=cost_center,
            supplier_ruc=supplier_ruc,
            amount=amount_pen_cents,
        ):
            continue
        current = best.get(rule.level)
        if current is None or (rule.specificity, -rule.id) > (current.specificity, -current.id):
            best[rule.level] = rule
    return [
        Step(level, r.approver, r.id, r.sla_hours, r.escalate_to)
        for level, r in sorted(best.items())
    ]


@dataclass(frozen=True)
class Delegation:
    username: str
    delegate: str
    starts_on: date
    ends_on: date


def effective_approver(username: str, delegations: Sequence[Delegation], today: date) -> str:
    """Quién aprueba hoy en nombre de ``username`` (vacaciones, licencias). Sigue cadenas
    de delegación (A → B → C) y corta los ciclos."""
    seen = {username}
    current = username
    for _ in range(5):
        active = next(
            (d for d in delegations if d.username == current and d.starts_on <= today <= d.ends_on),
            None,
        )
        if active is None or active.delegate in seen:
            return current
        seen.add(active.delegate)
        current = active.delegate
    return current
