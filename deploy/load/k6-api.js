// Prueba de carga de la API (k6). Mide el p95 de las consultas y de la recepción de
// documentos con la cola Redis (la API solo guarda y encola: el trabajo lo hacen los workers).
//
//   k6 run -e BASE_URL=https://facturas-staging.empresa.pe -e API_KEY=ibk_… deploy/load/k6-api.js
//
// Úsala contra un entorno de pruebas: crea comprobantes de ejemplo.
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.BASE_URL;
const HEADERS = { Authorization: `Bearer ${__ENV.API_KEY}` };

export const options = {
  scenarios: {
    consultas: { executor: 'constant-vus', vus: 20, duration: '3m', exec: 'consultas' },
    recepcion: { executor: 'constant-arrival-rate', rate: 2, timeUnit: '1s', duration: '3m', preAllocatedVUs: 10, exec: 'recepcion' },
  },
  thresholds: {
    'http_req_duration{scenario:consultas}': ['p(95)<500'],
    'http_req_duration{scenario:recepcion}': ['p(95)<1500'],
    http_req_failed: ['rate<0.01'],
  },
};

export function consultas() {
  const res = http.get(`${BASE}/api/v1/invoices?limit=50`, { headers: HEADERS });
  check(res, { '200': (r) => r.status === 200 });
  sleep(1);
}

export function recepcion() {
  const n = String(Math.floor(Math.random() * 1e8)).padStart(8, '0');
  const xml = `<?xml version="1.0" encoding="UTF-8"?><Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"><cbc:ID>F999-${n}</cbc:ID><cbc:IssueDate>2026-10-15</cbc:IssueDate><cbc:InvoiceTypeCode>01</cbc:InvoiceTypeCode><cbc:DocumentCurrencyCode>PEN</cbc:DocumentCurrencyCode><cac:AccountingSupplierParty><cac:Party><cac:PartyIdentification><cbc:ID schemeID="6">20100047218</cbc:ID></cac:PartyIdentification></cac:Party></cac:AccountingSupplierParty><cac:LegalMonetaryTotal><cbc:PayableAmount currencyID="PEN">118.00</cbc:PayableAmount></cac:LegalMonetaryTotal></Invoice>`;
  const res = http.post(`${BASE}/api/v1/documents`, { files: http.file(xml, `F999-${n}.xml`, 'application/xml') }, { headers: HEADERS });
  check(res, { 'aceptado': (r) => r.status === 202 || r.status === 201 });
}
