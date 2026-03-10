// ── Shared utilities ─────────────────────────────────────────────────────────

const API = {
  base: '',

  headers() {
    const token = localStorage.getItem('token');
    return {
      'Content-Type': 'application/json',
      ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
    };
  },

  async get(path) {
    const r = await fetch(this.base + path, { headers: this.headers() });
    if (r.status === 401) { logout(); return; }
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },

  async post(path, body) {
    const r = await fetch(this.base + path, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify(body),
    });
    if (r.status === 401) { logout(); return; }
    if (!r.ok) { const t = await r.text(); throw new Error(JSON.parse(t)?.detail || t); }
    return r.json();
  },

  async postForm(path, formData) {
    const token = localStorage.getItem('token');
    const r = await fetch(this.base + path, {
      method: 'POST',
      headers: token ? { 'Authorization': `Bearer ${token}` } : {},
      body: formData,
    });
    if (r.status === 401) { logout(); return; }
    if (!r.ok) { const t = await r.text(); throw new Error(JSON.parse(t)?.detail || t); }
    return r.json();
  },

  async del(path) {
    const r = await fetch(this.base + path, {
      method: 'DELETE',
      headers: this.headers(),
    });
    if (r.status === 401) { logout(); return; }
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
};

function getUser() {
  const u = localStorage.getItem('user');
  return u ? JSON.parse(u) : null;
}

function logout() {
  localStorage.removeItem('token');
  localStorage.removeItem('user');
  window.location.href = '/';
}

function requireAuth() {
  if (!localStorage.getItem('token')) {
    window.location.href = '/';
    return false;
  }
  return true;
}

function showToast(msg, duration = 2500) {
  let t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    t.className = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('show'), duration);
}

// ── Session ID (재접속/새 탭마다 갱신) ────────────────────────────────────────
if (!sessionStorage.getItem('session_id')) {
  sessionStorage.setItem('session_id', crypto.randomUUID());
}
function getSessionId() { return sessionStorage.getItem('session_id'); }

// ── Event logging (fire-and-forget to NeonDB) ─────────────────────────────────
function logEvent(eventType, data = {}) {
  const token = localStorage.getItem('token');
  if (!token) return;
  fetch('/api/log', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
    body: JSON.stringify({ event_type: eventType, session_id: getSessionId(), ...data }),
  }).catch(() => {});
}

// User color palette (consistent per user_id)
const USER_COLORS = [
  '#E53E3E','#D97706','#059669','#2563EB',
  '#7C3AED','#DB2777','#0891B2','#65A30D',
];
function userColor(userId) {
  return USER_COLORS[userId % USER_COLORS.length];
}
