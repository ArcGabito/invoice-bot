'use strict';

document.getElementById('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const error = document.getElementById('login-error');
  error.textContent = '';
  const body = { username: form.username.value, password: form.password.value };
  try {
    const res = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
      credentials: 'same-origin',
    });
    if (res.ok) {
      window.location.assign('/');
      return;
    }
    const data = await res.json().catch(() => ({}));
    error.textContent = typeof data.detail === 'string' ? data.detail : 'No se pudo iniciar sesión';
  } catch {
    error.textContent = 'No se pudo contactar con el servidor';
  }
});
