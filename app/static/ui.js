// Shared by every page. Pages that need the status read window.appStatus (a promise of /api/status).
window.appStatus = fetch('/api/status').then(r => r.json()).catch(() => null);
