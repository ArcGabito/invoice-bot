'use strict';

// Pantalla de revisión: bandeja, datos editables, incidencias, historial y documento.
// Sin dependencias externas ni innerHTML con datos: todo el texto se inserta con textContent.

const csrf = document.querySelector('meta[name="csrf-token"]').content;
const state = { view: 'pending', selected: null, meta: null, detail: null };

const STATUS_LABELS = {
  valid: 'válida', warning: 'advertencias', error: 'errores',
  pending: 'pendiente', approved: 'aprobada', rejected: 'rechazada',
};
const SEVERITY_LABELS = { error: 'Error', warning: 'Advertencia', info: 'Info' };
const AMOUNT_FIELDS = new Set([
  'subtotal', 'taxable_amount', 'exempt_amount', 'unaffected_amount', 'discount_amount', 'igv',
  'isc', 'icbper', 'other_charges', 'other_taxes', 'total', 'prepaid_amount', 'detraction_amount',
]);
const FIELD_LABELS = {
  supplier_name: 'Emisor', supplier_ruc: 'RUC emisor', supplier_tax_id: 'ID fiscal (exterior)',
  supplier_country: 'País emisor', customer_name: 'Cliente', customer_ruc: 'RUC/DNI cliente',
  document_type: 'Tipo', series: 'Serie', number: 'Número', issue_date: 'Emisión',
  due_date: 'Vencimiento', currency: 'Moneda', detraction_code: 'Cód. detracción',
  detraction_percent: '% detracción', detraction_account: 'Cta. detracciones', subtotal: 'Subtotal',
  taxable_amount: 'Op. gravadas', exempt_amount: 'Op. exoneradas', unaffected_amount: 'Op. inafectas',
  discount_amount: 'Descuentos', igv: 'IGV', isc: 'ISC', icbper: 'ICBPER', other_charges: 'Otros cargos',
  other_taxes: 'Otros tributos', total: 'Total', prepaid_amount: 'Anticipos', detraction_amount: 'Detracción (S/)',
};

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function toast(message) {
  const box = document.getElementById('toast');
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, 5000);
}

async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: { 'content-type': 'application/json', 'x-csrf-token': csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) {
    window.location.assign('/login');
    throw new Error('Sesión vencida');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Error ${res.status}`);
  return data;
}

const cents = (value) => (value === null || value === undefined ? '' : (Number(value) / 100).toFixed(2));
const badge = (status) => el('span', { class: `badge ${status}` }, STATUS_LABELS[status] ?? status);
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString('es-PE') : '');

async function loadQueue() {
  const data = await api(`/api/v1/queue?view=${state.view}`);
  for (const [key, value] of Object.entries(data.counts)) {
    const counter = document.querySelector(`[data-count="${key}"]`);
    if (counter) counter.textContent = value ? `(${value})` : '';
  }
  const list = document.getElementById('queue-list');
  list.replaceChildren(
    ...data.items.map((item) =>
      el(
        'li',
        { class: item.invoice_id === state.selected ? 'selected' : '', onclick: () => select(item.invoice_id) },
        el('div', {}, badge(item.validation_status), ' ', el('strong', {}, `${item.series ?? ''}-${item.number ?? ''}`)),
        el('div', { class: 'small' }, item.supplier_name ?? item.file_name),
        el('div', { class: 'small muted' }, `${item.issue_date ?? 's/f'} · ${item.currency ?? ''} ${cents(item.total_cents)}`),
      ),
    ),
  );
  if (!data.items.length) list.append(el('li', { class: 'muted' }, 'Nada por aquí.'));
  const documents = [...(data.failed ?? []), ...(data.needs_ocr ?? [])];
  const box = document.getElementById('documents-box');
  box.hidden = documents.length === 0;
  document.getElementById('documents-list').replaceChildren(
    ...documents.map((d) => el('li', { class: 'small' }, `${d.file_name}: ${d.status === 'needs_ocr' ? 'requiere OCR' : d.error_message ?? d.error_kind}`)),
  );
}

async function select(invoiceId) {
  state.selected = invoiceId;
  document.querySelectorAll('#queue-list li').forEach((li) => li.classList.remove('selected'));
  state.detail = await api(`/api/v1/invoices/${invoiceId}`);
  renderDetail();
  renderPreview();
  loadQueue().catch(() => {});
}

function renderPreview() {
  const { invoice } = state.detail;
  const src = `/api/v1/invoices/${invoice.id}/document`;
  const preview = document.getElementById('preview');
  if (invoice.mime_type.startsWith('image/')) preview.replaceChildren(el('img', { src, alt: invoice.file_name }));
  else preview.replaceChildren(el('iframe', { src, title: invoice.file_name }));
}

function fieldValue(invoice, field) {
  if (AMOUNT_FIELDS.has(field)) return cents(invoice[`${field}_cents`]);
  const value = invoice[field];
  return value === null || value === undefined ? '' : String(value);
}

function renderDetail() {
  const { invoice, issues, actions, mail_origins: origins } = state.detail;
  const fields = state.meta.fields.map((field) =>
    el('label', {}, FIELD_LABELS[field] ?? field,
      el('input', { name: field, value: fieldValue(invoice, field), dataset: { original: fieldValue(invoice, field) },
        oninput: (e) => e.target.classList.toggle('changed', e.target.value !== e.target.dataset.original) })),
  );
  const form = el('form', { class: 'fields', onsubmit: (e) => e.preventDefault() }, fields);
  const note = el('input', { placeholder: 'Nota (obligatoria para aprobar con errores o rechazar)', maxlength: '1000' });
  const rejected = invoice.review_status === 'rejected';
  const run = (fn) => async () => {
    try {
      const result = await fn();
      toast(result?.validation_status ? `Hecho: ${STATUS_LABELS[result.validation_status]}` : 'Hecho');
      await select(invoice.id);
    } catch (error) {
      toast(error.message);
    }
  };
  const actionsBar = el('div', { class: 'actions' },
    el('button', { class: 'primary', disabled: rejected ? '' : null, onclick: run(() => {
      const changes = {};
      form.querySelectorAll('input.changed').forEach((input) => { changes[input.name] = input.value; });
      if (!Object.keys(changes).length) throw new Error('No cambiaste ningún dato');
      return api(`/api/v1/invoices/${invoice.id}/correct`, { method: 'POST', body: { changes, note: note.value || null } });
    }) }, 'Guardar correcciones'),
    el('button', { disabled: rejected ? '' : null, onclick: run(() => api(`/api/v1/invoices/${invoice.id}/approve`, {
      method: 'POST', body: { note: note.value || null, force: issues.some((i) => i.severity === 'error' && !i.resolved_at) },
    })) }, 'Aprobar'),
    el('button', { class: 'danger', disabled: rejected ? '' : null, onclick: run(() => api(`/api/v1/invoices/${invoice.id}/reject`, {
      method: 'POST', body: { reason: note.value },
    })) }, 'Rechazar'),
    invoice.review_status !== 'pending'
      ? el('button', { onclick: run(() => api(`/api/v1/invoices/${invoice.id}/reopen`, { method: 'POST', body: { note: note.value || null } })) }, 'Reabrir')
      : null,
  );
  const issueList = el('ul', { class: 'issues' }, issues.map((issue) =>
    el('li', { class: `${issue.severity} ${issue.resolved_at ? 'resolved' : ''}` },
      el('strong', {}, `${SEVERITY_LABELS[issue.severity]} · ${issue.rule_code}`), ' ', issue.message,
      issue.expected_value || issue.actual_value
        ? el('div', { class: 'small muted' }, `esperado: ${issue.expected_value ?? '—'} · encontrado: ${issue.actual_value ?? '—'}`)
        : null,
      issue.resolved_at
        ? el('div', { class: 'small muted' }, `Resuelta por ${issue.resolved_by} ${fmtDate(issue.resolved_at)} ${issue.resolution_note ?? ''}`)
        : issue.severity !== 'info'
          ? el('button', { class: 'small', onclick: run(() => api(`/api/v1/issues/${issue.id}/resolve`, { method: 'POST', body: { note: note.value || null } })) }, 'Dar por resuelta')
          : null,
    )));
  const history = el('table', { class: 'history' },
    el('tr', {}, el('th', {}, 'Cuándo'), el('th', {}, 'Quién'), el('th', {}, 'Acción'), el('th', {}, 'Detalle')),
    actions.map((a) => el('tr', {},
      el('td', {}, fmtDate(a.created_at)), el('td', {}, a.user), el('td', {}, a.action),
      el('td', {}, [a.field, a.old_value !== null || a.new_value !== null ? `${a.old_value ?? ''} → ${a.new_value ?? ''}` : '', a.note].filter(Boolean).join(' · ')))));
  document.getElementById('detail').replaceChildren(
    el('h2', {}, `${invoice.series ?? ''}-${invoice.number ?? ''} `, badge(invoice.validation_status), ' ', badge(invoice.review_status)),
    el('div', { class: 'muted small' },
      `${invoice.file_name} · origen ${invoice.data_source} · SUNAT ${invoice.sunat_status ?? 'sin consultar'}`,
      invoice.exchange_rate ? ` · T.C. ${invoice.exchange_rate} (${invoice.exchange_rate_date})` : ''),
    origins.length
      ? el('div', { class: 'small muted' }, `Llegó por correo: ${origins.map((o) => `${o.sender ?? '?'} · «${o.subject ?? ''}»`).join('; ')}`)
      : null,
    form, note, actionsBar,
    el('h3', {}, 'Incidencias'), issueList,
    el('h3', {}, 'Historial'), history,
  );
}

async function main() {
  state.meta = await api('/api/v1/meta');
  const month = new Date().toISOString().slice(0, 7);
  document.getElementById('export-link').href = `/api/v1/exports?format=xlsx&month=${month}`;
  document.getElementById('tabs').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-view]');
    if (!button) return;
    state.view = button.dataset.view;
    document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('active', b === button));
    loadQueue().catch((e) => toast(e.message));
  });
  document.getElementById('logout').addEventListener('click', async () => {
    await api('/api/v1/auth/logout', { method: 'POST' }).catch(() => {});
    window.location.assign('/login');
  });
  document.getElementById('run-button').addEventListener('click', async () => {
    try {
      await api('/api/v1/runs', { method: 'POST' });
      toast('Ciclo lanzado: la bandeja se actualizará al terminar');
      setTimeout(() => loadQueue().catch(() => {}), 15000);
    } catch (error) {
      toast(error.message);
    }
  });
  await loadQueue();
  setInterval(() => loadQueue().catch(() => {}), 60000);
}

main().catch((error) => toast(error.message));
