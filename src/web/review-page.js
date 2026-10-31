const escapeHtml = (text) => String(text).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

/**
 * Página única de la bandeja de revisión. Todo el contenido dinámico se
 * inserta con textContent (nunca como HTML) y el script y los estilos llevan
 * el nonce de la política de seguridad de contenido.
 */
export function renderReviewPage({ token, nonce, reviewer }) {
  return `<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="invoicebot-token" content="${escapeHtml(token)}">
<title>InvoiceBot · Revisión</title>
<style nonce="${nonce}">
  :root { --bg:#f6f7f9; --panel:#fff; --text:#1d2330; --muted:#667085; --line:#e4e7ec; --accent:#2457d6;
    --err:#b42318; --err-bg:#fef3f2; --warn:#b54708; --warn-bg:#fffaeb; --ok:#067647; --ok-bg:#ecfdf3; --info:#475467; --info-bg:#f2f4f7; }
  @media (prefers-color-scheme: dark) { :root { --bg:#111418; --panel:#1a1f26; --text:#e6e9ee; --muted:#98a2b3; --line:#2b323c;
    --accent:#7aa2ff; --err:#ff8a7a; --err-bg:#3a1d1a; --warn:#fdb022; --warn-bg:#352a12; --ok:#47cd89; --ok-bg:#12301f; --info:#cdd5df; --info-bg:#262c35; } }
  * { box-sizing: border-box; }
  body { margin:0; font: 14px/1.45 system-ui, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }
  header { display:flex; flex-wrap:wrap; gap:12px; align-items:center; padding:10px 16px; background:var(--panel); border-bottom:1px solid var(--line); }
  header h1 { font-size:16px; margin:0 12px 0 0; }
  .tabs { display:flex; gap:4px; flex-wrap:wrap; }
  .tabs button, .btn { border:1px solid var(--line); background:var(--panel); color:var(--text); padding:6px 10px; border-radius:6px; cursor:pointer; font:inherit; }
  .tabs button[aria-pressed="true"] { border-color:var(--accent); color:var(--accent); font-weight:600; }
  .who { margin-left:auto; color:var(--muted); }
  main { display:grid; grid-template-columns: 300px minmax(380px, 1fr) minmax(320px, 1fr); height: calc(100vh - 53px); }
  @media (max-width: 1100px) { main { grid-template-columns: 1fr; height:auto; } #doc { height:70vh; } }
  #list { overflow:auto; border-right:1px solid var(--line); background:var(--panel); }
  #list .item { padding:10px 12px; border-bottom:1px solid var(--line); cursor:pointer; }
  #list .item[aria-selected="true"] { background:var(--info-bg); }
  #list .item .top { display:flex; justify-content:space-between; gap:8px; }
  #list .sub { color:var(--muted); font-size:12px; }
  #list .empty { padding:24px 12px; color:var(--muted); }
  #detail { overflow:auto; padding:16px; }
  #doc { border-left:1px solid var(--line); background:var(--panel); }
  #doc { display:flex; flex-direction:column; }
  #doc iframe { width:100%; flex:1; border:0; background:#fff; }
  .doc-bar { display:flex; justify-content:space-between; gap:8px; padding:8px 12px; border-bottom:1px solid var(--line); font-size:12px; color:var(--muted); }
  .doc-bar a { color:var(--accent); }
  .placeholder { color:var(--muted); padding:24px; }
  h2 { font-size:15px; margin:0 0 4px; }
  h3 { font-size:13px; margin:18px 0 6px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  .badges { display:flex; gap:6px; flex-wrap:wrap; margin:6px 0 12px; }
  .badge { font-size:12px; padding:1px 8px; border-radius:999px; background:var(--info-bg); color:var(--info); }
  .badge.error { background:var(--err-bg); color:var(--err); } .badge.warning { background:var(--warn-bg); color:var(--warn); }
  .badge.valid, .badge.approved { background:var(--ok-bg); color:var(--ok); } .badge.rejected { background:var(--err-bg); color:var(--err); }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap:8px 12px; }
  label { display:flex; flex-direction:column; gap:2px; font-size:12px; color:var(--muted); }
  input, textarea { font:inherit; color:var(--text); background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:5px 7px; }
  input.changed { border-color:var(--accent); background:var(--panel); }
  input.amount { text-align:right; font-variant-numeric: tabular-nums; }
  textarea { width:100%; min-height:44px; resize:vertical; }
  .actions { display:flex; gap:8px; flex-wrap:wrap; margin:10px 0; }
  .btn.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  .btn.danger { color:var(--err); border-color:var(--err); }
  .btn:disabled { opacity:.5; cursor:default; }
  .issue { border:1px solid var(--line); border-left:4px solid var(--info); border-radius:6px; padding:8px 10px; margin-bottom:6px; display:flex; gap:10px; justify-content:space-between; align-items:flex-start; background:var(--panel); }
  .issue.error { border-left-color:var(--err); } .issue.warning { border-left-color:var(--warn); }
  .issue .code { font-size:11px; color:var(--muted); }
  .history li { color:var(--muted); margin-bottom:2px; }
  #toast { position:fixed; bottom:16px; left:50%; transform:translateX(-50%); padding:10px 14px; border-radius:8px; background:var(--text); color:var(--bg); display:none; max-width:90vw; }
  #toast.error { background:var(--err); color:#fff; }
  .note { font-size:12px; color:var(--muted); }
</style>
</head>
<body>
<header>
  <h1>InvoiceBot · Revisión</h1>
  <nav class="tabs" aria-label="Vistas">
    <button type="button" data-view="pending" aria-pressed="true">Por revisar <span data-count="pending"></span></button>
    <button type="button" data-view="approved" aria-pressed="false">Aprobadas <span data-count="approved"></span></button>
    <button type="button" data-view="rejected" aria-pressed="false">Rechazadas <span data-count="rejected"></span></button>
  </nav>
  <span class="who">Revisando como <strong>${escapeHtml(reviewer)}</strong></span>
</header>
<main>
  <section id="list" aria-label="Facturas"></section>
  <section id="detail"><p class="placeholder">Elige una factura de la lista.</p></section>
  <section id="doc" aria-label="Documento original"><p class="placeholder">Aquí verás el documento original.</p></section>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<script nonce="${nonce}">
(() => {
  const TOKEN = document.querySelector('meta[name="invoicebot-token"]').content;
  const LABELS = {
    supplier_name: 'Emisor', supplier_ruc: 'RUC emisor', supplier_tax_id: 'ID fiscal (exterior)', supplier_country: 'País emisor',
    customer_name: 'Cliente', customer_ruc: 'RUC/DNI cliente', document_type: 'Tipo (01, 03, 07, 08, 91…)', series: 'Serie',
    number: 'Número', issue_date: 'Fecha de emisión', due_date: 'Vencimiento', currency: 'Moneda',
    subtotal: 'Subtotal', taxable_amount: 'Op. gravadas', exempt_amount: 'Op. exoneradas', unaffected_amount: 'Op. inafectas',
    discount_amount: 'Descuentos', igv: 'IGV', isc: 'ISC', icbper: 'ICBPER', other_charges: 'Otros cargos',
    other_taxes: 'Otros tributos', total: 'Total', prepaid_amount: 'Anticipos',
  };
  const REVIEW = { pending: 'Pendiente', approved: 'Aprobada', rejected: 'Rechazada' };
  const VALIDATION = { valid: 'Válida', warning: 'Con advertencias', error: 'Con errores' };
  const ACTION = { approve: 'aprobó', reject: 'rechazó', reopen: 'reabrió', correct: 'corrigió', resolve_issue: 'resolvió' };
  const state = { view: 'pending', items: [], selected: null, detail: null, meta: null };

  const $ = (sel) => document.querySelector(sel);
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else if (v !== false && v !== null && v !== undefined) node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c);
    return node;
  }
  const nodes = (...items) => items.flat(Infinity).filter((x) => x !== null && x !== undefined && x !== false);
  const money = (cents) => (cents === null || cents === undefined ? '' : (cents / 100).toFixed(2));
  const fieldValue = (inv, f) => (f in inv ? inv[f] ?? '' : money(inv[f + '_cents']));

  function toast(message, isError = false) {
    const t = $('#toast');
    t.textContent = message;
    t.className = isError ? 'error' : '';
    t.style.display = 'block';
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => (t.style.display = 'none'), isError ? 6000 : 3000);
  }

  async function api(path, body) {
    const res = await fetch(path, {
      method: body ? 'POST' : 'GET',
      headers: { 'x-invoicebot-token': TOKEN, ...(body ? { 'content-type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || 'Error ' + res.status);
    return data;
  }

  async function loadQueue() {
    const data = await api('/api/queue?view=' + state.view);
    state.items = data.items;
    for (const [k, n] of Object.entries(data.counts)) {
      const span = document.querySelector('[data-count="' + k + '"]');
      if (span) span.textContent = '(' + n + ')';
    }
    renderList();
  }

  function renderList() {
    const list = $('#list');
    list.replaceChildren();
    if (!state.items.length) {
      list.append(el('p', { class: 'empty', text: state.view === 'pending' ? 'No hay facturas por revisar. 🎉' : 'No hay facturas en esta vista.' }));
      return;
    }
    for (const it of state.items) {
      const id = it.invoice_id;
      const status = state.view === 'pending' ? it.validation_status : it.review_status;
      list.append(el('div', {
        class: 'item', role: 'button', tabindex: '0', 'aria-selected': String(state.selected === id),
        onclick: () => select(id), onkeydown: (e) => { if (e.key === 'Enter') select(id); },
      },
        el('div', { class: 'top' },
          el('strong', { text: '#' + id + ' ' + [it.series, it.number].filter(Boolean).join('-') }),
          el('span', { class: 'badge ' + status, text: state.view === 'pending' ? VALIDATION[status] : REVIEW[status] })),
        el('div', { text: it.supplier_name || it.supplier_ruc || 'Emisor sin identificar' }),
        el('div', { class: 'sub', text: [it.issue_date, it.currency && it.currency + ' ' + money(it.total_cents)].filter(Boolean).join(' · ') }),
        it.issues && it.issues[0] ? el('div', { class: 'sub', text: it.issues[0].message }) : null,
      ));
    }
  }

  async function select(id) {
    state.selected = id;
    renderList();
    try {
      state.detail = await api('/api/invoices/' + id);
      renderDetail();
      renderDocument();
    } catch (e) { toast(e.message, true); }
  }

  function renderDocument() {
    const src = '/api/invoices/' + state.detail.invoice.id + '/document?token=' + encodeURIComponent(TOKEN);
    // Algunos navegadores no muestran PDF dentro de un marco: el enlace siempre funciona.
    $('#doc').replaceChildren(
      el('div', { class: 'doc-bar' },
        el('span', { text: state.detail.invoice.file_name }),
        el('a', { href: src, target: '_blank', rel: 'noopener', text: 'Abrir en otra pestaña' })),
      el('iframe', { title: 'Documento original', src }),
    );
  }

  function renderDetail() {
    const { invoice: inv, issues, actions } = state.detail;
    const open = issues.filter((i) => !i.resolved_at);
    const resolved = issues.filter((i) => i.resolved_at);
    const openErrors = open.filter((i) => i.severity === 'error');
    const inputs = {};
    const note = el('textarea', { id: 'note', placeholder: 'Nota: motivo de la corrección, del rechazo o de aprobar con errores' });

    const fields = state.meta.fields.map((f) => {
      const amount = !(f in inv);
      const original = String(fieldValue(inv, f));
      const input = el('input', { name: f, value: original, class: amount ? 'amount' : '', inputmode: amount ? 'decimal' : null,
        disabled: inv.review_status === 'rejected',
        oninput: () => input.classList.toggle('changed', input.value !== original) });
      inputs[f] = { input, original };
      return el('label', {}, LABELS[f] || f, input);
    });

    const run = async (fn, okMessage) => {
      try { await fn(); toast(okMessage); await loadQueue(); await select(inv.id); }
      catch (e) { toast(e.message, true); }
    };
    const save = () => {
      const changes = Object.fromEntries(Object.entries(inputs)
        .filter(([, v]) => v.input.value.trim() !== v.original.trim()).map(([f, v]) => [f, v.input.value]));
      if (!Object.keys(changes).length) return toast('No hay cambios que guardar');
      run(() => api('/api/invoices/' + inv.id + '/correct', { changes, note: note.value }), 'Correcciones guardadas y factura revalidada');
    };
    const approve = () => {
      if (openErrors.length && !note.value.trim()) return toast('Escribe en la nota por qué apruebas con errores', true);
      run(() => api('/api/invoices/' + inv.id + '/approve', { note: note.value, force: openErrors.length > 0 }), 'Factura aprobada');
    };
    const reject = () => {
      if (!note.value.trim()) return toast('Escribe en la nota el motivo del rechazo', true);
      run(() => api('/api/invoices/' + inv.id + '/reject', { reason: note.value }), 'Factura rechazada');
    };
    const reopen = () => run(() => api('/api/invoices/' + inv.id + '/reopen', { note: note.value }), 'Factura reabierta');
    const resolve = (issue) => {
      if (issue.severity === 'error' && !note.value.trim()) return toast('Escribe en la nota por qué das el error por resuelto', true);
      run(() => api('/api/issues/' + issue.id + '/resolve', { note: note.value }), 'Incidencia resuelta');
    };

    const pending = inv.review_status === 'pending';
    // replaceChildren no aplana listas y escribiría "null": se limpia antes.
    $('#detail').replaceChildren(...nodes(
      el('h2', { text: '#' + inv.id + ' · ' + [inv.series, inv.number].filter(Boolean).join('-') + ' · ' + (inv.supplier_name || '') }),
      el('div', { class: 'note', text: inv.file_name + ' · origen: ' + (inv.data_source === 'xml' ? 'XML (exacto)' : 'IA') + (inv.sunat_status ? ' · SUNAT: ' + inv.sunat_status : '') }),
      el('div', { class: 'badges' },
        el('span', { class: 'badge ' + inv.review_status, text: REVIEW[inv.review_status] + (inv.reviewed_by ? ' por ' + inv.reviewed_by : '') }),
        el('span', { class: 'badge ' + inv.validation_status, text: VALIDATION[inv.validation_status] }),
        inv.corrected ? el('span', { class: 'badge', text: 'Corregida a mano' }) : null),
      el('h3', { text: 'Incidencias abiertas (' + open.length + ')' }),
      open.length ? open.map((i) => el('div', { class: 'issue ' + i.severity },
        el('div', {},
          el('div', { text: i.message }),
          el('div', { class: 'code', text: i.rule_code + [i.expected_value && ' · esperado ' + i.expected_value, i.actual_value && ' · encontrado ' + i.actual_value].filter(Boolean).join('') })),
        pending ? el('button', { class: 'btn', type: 'button', text: 'Resolver', onclick: () => resolve(i) }) : null))
        : el('p', { class: 'note', text: 'Sin incidencias abiertas.' }),
      el('h3', { text: 'Datos' }),
      el('div', { class: 'grid' }, fields),
      el('h3', { text: 'Acción' }),
      note,
      el('div', { class: 'actions' },
        inv.review_status !== 'rejected' ? el('button', { class: 'btn', type: 'button', text: 'Guardar correcciones', onclick: save }) : null,
        pending ? el('button', { class: 'btn primary', type: 'button', text: openErrors.length ? 'Aprobar con errores' : 'Aprobar', onclick: approve }) : null,
        pending ? el('button', { class: 'btn danger', type: 'button', text: 'Rechazar', onclick: reject }) : null,
        !pending ? el('button', { class: 'btn', type: 'button', text: 'Reabrir', onclick: reopen }) : null),
      resolved.length ? el('h3', { text: 'Incidencias resueltas (' + resolved.length + ')' }) : null,
      resolved.map((i) => el('div', { class: 'note', text: '✓ ' + i.rule_code + ' · ' + (i.resolved_by || '') + (i.resolution_note ? ': ' + i.resolution_note : '') })),
      actions.length ? el('h3', { text: 'Historial' }) : null,
      actions.length ? el('ul', { class: 'history' }, actions.map((a) => el('li', {
        text: new Date(a.created_at).toLocaleString('es-PE') + ' · ' + a.user + ' ' + ACTION[a.action] +
          (a.action === 'correct' ? ' ' + LABELS[a.field] + ': "' + (a.old_value || '') + '" → "' + (a.new_value || '') + '"' : '') +
          (a.action === 'resolve_issue' ? ' ' + a.field : '') + (a.note ? ' — ' + a.note : ''),
      }))) : null,
    ));
  }

  for (const button of document.querySelectorAll('[data-view]')) {
    button.addEventListener('click', async () => {
      state.view = button.dataset.view;
      for (const b of document.querySelectorAll('[data-view]')) b.setAttribute('aria-pressed', String(b === button));
      await loadQueue();
    });
  }

  (async () => {
    try {
      state.meta = await api('/api/meta');
      await loadQueue();
      if (state.items[0]) await select(state.items[0].invoice_id);
    } catch (e) { toast(e.message, true); }
  })();
})();
</script>
</body>
</html>`;
}
