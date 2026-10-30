import { SEVERITY } from './invoice.js';

/**
 * Interpretación de la respuesta de la Consulta Integrada de Comprobante de
 * Pago de SUNAT (anexo del manual v2.0). Es una función pura: no consulta
 * nada, solo traduce códigos a incidencias.
 */

export const RECEIPT_STATUS = Object.freeze({
  0: 'NO EXISTE',
  1: 'ACEPTADO',
  2: 'ANULADO',
  3: 'AUTORIZADO',
  4: 'NO AUTORIZADO',
});

export const TAXPAYER_STATUS = Object.freeze({
  '00': 'ACTIVO',
  '01': 'BAJA PROVISIONAL',
  '02': 'BAJA PROV. POR OFICIO',
  '03': 'SUSPENSION TEMPORAL',
  10: 'BAJA DEFINITIVA',
  11: 'BAJA DE OFICIO',
  22: 'INHABILITADO-VENT.UNICA',
});

export const DOMICILE_CONDITION = Object.freeze({
  '00': 'HABIDO',
  '09': 'PENDIENTE',
  11: 'POR VERIFICAR',
  12: 'NO HABIDO',
  20: 'NO HALLADO',
});

/** Prefijo de las incidencias generadas por SUNAT: se reemplazan en cada nueva consulta. */
export const SUNAT_RULE_PREFIX = 'SUNAT_';

const issue = (rule_code, severity, message, actual = null) => ({
  rule_code,
  severity,
  message,
  expected_value: null,
  actual_value: actual,
});

/**
 * @param {{ estadoCp: string | null, estadoRuc: string | null, condDomiRuc: string | null, observaciones: string[] }} result
 * @param {{ documentType: string }} invoice
 * @returns {{ receiptStatus: string | null, issues: object[] }}
 */
export function evaluateSunatResult(result, { documentType }) {
  const issues = [];
  const receiptStatus = RECEIPT_STATUS[result.estadoCp] ?? null;

  switch (result.estadoCp) {
    case '0':
      // Las boletas se informan en el resumen diario y pueden tardar unos días
      // en aparecer; una factura no informada es un problema.
      issues.push(
        issue(
          'SUNAT_RECEIPT_NOT_FOUND',
          documentType === '03' ? SEVERITY.WARNING : SEVERITY.ERROR,
          documentType === '03'
            ? 'SUNAT todavía no tiene informada esta boleta (puede llegar en el resumen diario); vuelve a consultar en unos días'
            : 'SUNAT no tiene informado este comprobante: no debe registrarse hasta confirmarlo con el proveedor',
          receiptStatus,
        ),
      );
      break;
    case '2':
      issues.push(
        issue(
          'SUNAT_RECEIPT_VOIDED',
          SEVERITY.ERROR,
          'El comprobante fue anulado (comunicado de baja ante SUNAT)',
          receiptStatus,
        ),
      );
      break;
    case '4':
      issues.push(
        issue(
          'SUNAT_RECEIPT_NOT_AUTHORIZED',
          SEVERITY.ERROR,
          'Comprobante físico no autorizado por imprenta',
          receiptStatus,
        ),
      );
      break;
    case '1':
    case '3':
      break;
    default:
      issues.push(
        issue(
          'SUNAT_RECEIPT_UNKNOWN_STATUS',
          SEVERITY.WARNING,
          'SUNAT devolvió un estado de comprobante desconocido',
          String(result.estadoCp),
        ),
      );
  }

  if (result.estadoRuc && result.estadoRuc !== '00') {
    issues.push(
      issue(
        'SUNAT_SUPPLIER_NOT_ACTIVE',
        SEVERITY.WARNING,
        'El RUC del emisor no está ACTIVO en SUNAT',
        TAXPAYER_STATUS[result.estadoRuc] ?? result.estadoRuc,
      ),
    );
  }
  if (result.condDomiRuc === '12' || result.condDomiRuc === '20') {
    issues.push(
      issue(
        'SUNAT_SUPPLIER_NOT_HABIDO',
        SEVERITY.WARNING,
        'El emisor tiene domicilio NO HABIDO o NO HALLADO: revisa con tu contador el uso del crédito fiscal',
        DOMICILE_CONDITION[result.condDomiRuc],
      ),
    );
  }
  for (const note of result.observaciones ?? []) {
    issues.push(issue('SUNAT_OBSERVATION', SEVERITY.INFO, String(note).slice(0, 500)));
  }
  return { receiptStatus, issues };
}
