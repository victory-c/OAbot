// Shared by every page: fills the connection chips in the header.
// Pages that need the status too read window.appStatus (a promise of /api/status).
window.appStatus = fetch('/api/status').then(r => r.json()).catch(() => null);

document.addEventListener('DOMContentLoaded', async () => {
  const el = document.getElementById('conn');
  const s = await window.appStatus;
  if (!el || !s) return;
  const esc = v => String(v ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const cal = s.calendar === 'google';
  el.innerHTML = `<li class="${s.gmail ? 'on' : ''}">Gmail ${s.gmail ? 'watching' : 'off'}</li>
    <li class="${cal ? 'on' : ''}">Calendar ${cal ? 'Google' : esc(s.calendar)}</li>
    <li class="${s.bcourses ? 'on' : ''}">bCourses ${s.bcourses ? 'connected' : 'mock'}</li>`;
});
