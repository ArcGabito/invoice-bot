'use strict';

const headers = { 'x-requested-with': 'portal' };

function money(currency, cents) {
  if (cents === null || cents === undefined) return '';
  return `${currency || 'PEN'} ${(cents / 100).toLocaleString('es-PE', { minimumFractionDigits: 2 })}`;
}

function cell(text) {
  const td = document.createElement('td');
  td.textContent = text === null || text === undefined ? '' : String(text);
  return td;
}

async function loadInvoices() {
  const res = await fetch('/portal/api/invoices', { credentials: 'same-origin' });
  if (!res.ok) return;
  const data = await res.json();
  const body = document.querySelector('#invoices tbody');
  body.replaceChildren();
  for (const item of data.items) {
    const row = document.createElement('tr');
    const stage = item.detail ? `${item.stage}: ${item.detail}` : item.stage;
    const payment = item.scheduled_payment_date
      ? `${item.scheduled_payment_date} · ${money(item.currency, item.payable_cents)}`
      : '';
    row.append(
      cell(item.invoice), cell(item.issue_date), cell(money(item.currency, item.total_cents)),
      cell(stage), cell(payment),
    );
    body.append(row);
  }
}

const loginForm = document.getElementById('login-form');
if (loginForm) {
  loginForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const status = document.getElementById('login-status');
    const res = await fetch('/portal/api/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ ruc: loginForm.ruc.value.trim(), email: loginForm.email.value.trim() }),
    });
    const data = await res.json().catch(() => ({}));
    status.textContent = res.ok ? data.message : (data.detail || 'No se pudo enviar el enlace');
  });
}

const uploadForm = document.getElementById('upload-form');
if (uploadForm) {
  loadInvoices();
  uploadForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const status = document.getElementById('upload-status');
    const form = new FormData(uploadForm);
    const res = await fetch('/portal/api/documents', {
      method: 'POST', headers, body: form, credentials: 'same-origin',
    });
    const data = await res.json().catch(() => ({}));
    status.textContent = res.ok ? `Recibimos ${data.files} archivo(s).` : (data.detail || 'No se pudo enviar');
    if (res.ok) { uploadForm.reset(); setTimeout(loadInvoices, 2000); }
  });
  document.getElementById('logout').addEventListener('click', async (event) => {
    event.preventDefault();
    await fetch('/portal/api/logout', { method: 'POST', headers, credentials: 'same-origin' });
    window.location.assign('/portal');
  });
}
