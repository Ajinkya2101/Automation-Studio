// Automation Studio sign-in page.
const form = document.getElementById('login-form');
const errBox = document.getElementById('login-error');
const btn = document.getElementById('login-btn');

form.addEventListener('submit', async e => {
  e.preventDefault();
  errBox.textContent = '';
  btn.disabled = true;
  try {
    const res = await fetch('/api/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: form.username.value, password: form.password.value }),
    });
    if (res.ok) { location.replace('/'); return; }
    const body = await res.json().catch(() => ({}));
    errBox.textContent = body.detail || `Sign-in failed (${res.status}).`;
    form.password.value = '';
    form.password.focus();
  } catch (err) {
    errBox.textContent = `The studio did not answer: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
});
