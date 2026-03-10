// ── Bootstrap ─────────────────────────────────────────────────────────────────
if (!requireAuth()) throw new Error('redirect');

pdfjsLib.GlobalWorkerOptions.workerSrc =
  'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

const articleId = parseInt(window.location.pathname.split('/').pop(), 10);
const currentUser = getUser();

// ── State ─────────────────────────────────────────────────────────────────────
let pdfDoc       = null;
let annotations  = [];          // all loaded annotations
let currentTool  = 'select';
let currentColor = '#FFD700';
let pendingNote  = null;        // { pageNum, x, y } waiting for modal input
let ws           = null;

// ── Tool selection ─────────────────────────────────────────────────────────────
const TOOLS = ['select', 'highlight', 'underline', 'note', 'erase'];

function setTool(name) {
  currentTool = name;
  TOOLS.forEach(t => document.getElementById(`tool-${t}`).classList.toggle('active', t === name));
  // update cursor on all annotation layers
  document.querySelectorAll('.annotation-layer').forEach(el => {
    el.classList.remove('note-mode', 'erase-mode');
    if (name === 'note')  el.classList.add('note-mode');
    if (name === 'erase') el.classList.add('erase-mode');
  });
}
TOOLS.forEach(t => document.getElementById(`tool-${t}`).addEventListener('click', () => setTool(t)));
setTool('select');

// ── Color swatches ─────────────────────────────────────────────────────────────
document.querySelectorAll('.color-swatch').forEach(sw => {
  sw.addEventListener('click', () => {
    document.querySelectorAll('.color-swatch').forEach(s => s.classList.remove('selected'));
    sw.classList.add('selected');
    currentColor = sw.dataset.color;
  });
});

// ── Sidebar toggle ─────────────────────────────────────────────────────────────
document.getElementById('sidebar-toggle').addEventListener('click', () => {
  document.getElementById('sidebar').classList.toggle('collapsed');
});

// ── Note modal ─────────────────────────────────────────────────────────────────
document.getElementById('note-cancel').addEventListener('click', () => {
  document.getElementById('note-modal').classList.remove('open');
  pendingNote = null;
});

document.getElementById('note-save').addEventListener('click', async () => {
  const text = document.getElementById('note-text').value.trim();
  if (!text || !pendingNote) return;
  document.getElementById('note-modal').classList.remove('open');
  const { pageNum, x, y } = pendingNote;
  pendingNote = null;
  document.getElementById('note-text').value = '';
  await saveAnnotation('note', pageNum, { x, y, text });
});

// ── Coordinate helpers ─────────────────────────────────────────────────────────
function normalizeRect(domRect, container) {
  const c = container.getBoundingClientRect();
  return {
    x: (domRect.left - c.left) / c.width,
    y: (domRect.top  - c.top)  / c.height,
    w: domRect.width  / c.width,
    h: domRect.height / c.height,
  };
}

function toPixels(nr, container) {
  return {
    x: nr.x * container.clientWidth,
    y: nr.y * container.clientHeight,
    w: nr.w * container.clientWidth,
    h: nr.h * container.clientHeight,
  };
}

// ── PDF loading ────────────────────────────────────────────────────────────────
async function loadPDF() {
  const token = localStorage.getItem('token');
  const pdfUrl = `/api/articles/${articleId}/pdf`;

  try {
    pdfDoc = await pdfjsLib.getDocument({
      url: pdfUrl,
      httpHeaders: { 'Authorization': `Bearer ${token}` },
    }).promise;

    document.getElementById('pdf-viewer').innerHTML = '';
    for (let pageNum = 1; pageNum <= pdfDoc.numPages; pageNum++) {
      await renderPage(pageNum);
    }
  } catch (err) {
    document.getElementById('pdf-viewer').innerHTML =
      `<p style="color:var(--danger);padding:20px">Failed to load PDF: ${err.message}</p>`;
  }
}

async function renderPage(pageNum) {
  const page    = await pdfDoc.getPage(pageNum);
  const viewer  = document.getElementById('pdf-viewer');
  const scale   = Math.min((viewer.clientWidth - 40) / page.getViewport({ scale: 1 }).width, 2.0);
  const viewport = page.getViewport({ scale });

  // wrapper
  const wrapper = document.createElement('div');
  wrapper.className = 'page-wrapper';
  wrapper.dataset.page = pageNum;
  wrapper.style.width  = `${viewport.width}px`;
  wrapper.style.height = `${viewport.height}px`;

  // canvas
  const canvas = document.createElement('canvas');
  canvas.width  = viewport.width;
  canvas.height = viewport.height;
  wrapper.appendChild(canvas);

  // text layer
  const textLayer = document.createElement('div');
  textLayer.className = 'textLayer';
  textLayer.style.width  = `${viewport.width}px`;
  textLayer.style.height = `${viewport.height}px`;
  wrapper.appendChild(textLayer);

  // annotation SVG layer
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.classList.add('annotation-layer');
  svg.setAttribute('width',  viewport.width);
  svg.setAttribute('height', viewport.height);
  svg.dataset.page = pageNum;
  wrapper.appendChild(svg);

  viewer.appendChild(wrapper);

  // render PDF canvas
  await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;

  // render text layer
  const textContent = await page.getTextContent();
  await pdfjsLib.renderTextLayer({
    textContentSource: textContent,
    container: textLayer,
    viewport,
    textDivs: [],
  }).promise;

  // attach event listeners
  setupPageEvents(wrapper, svg, pageNum);
}

// ── Page events ────────────────────────────────────────────────────────────────
function setupPageEvents(wrapper, svg, pageNum) {
  // Highlight / Underline: capture text selection on mouseup
  wrapper.addEventListener('mouseup', () => {
    if (currentTool !== 'highlight' && currentTool !== 'underline') return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return;
    const range = sel.getRangeAt(0);
    const domRects = Array.from(range.getClientRects()).filter(r => r.width > 2 && r.height > 2);
    if (!domRects.length) return;

    const rects = domRects.map(r => normalizeRect(r, wrapper));
    const selectedText = sel.toString().trim().slice(0, 200);
    sel.removeAllRanges();

    saveAnnotation(currentTool, pageNum, {
      rects,
      color: currentColor,
      selectedText,
    });
  });

  // Note: click on annotation layer
  svg.addEventListener('click', (e) => {
    if (currentTool !== 'note') return;
    // Don't trigger if clicking an existing annotation
    if (e.target !== svg && e.target.closest('.ann-group')) return;
    const rect = svg.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top)  / rect.height;
    pendingNote = { pageNum, x, y };
    document.getElementById('note-text').value = '';
    document.getElementById('note-modal').classList.add('open');
    document.getElementById('note-text').focus();
  });
}

// ── Render annotations onto SVG ────────────────────────────────────────────────
function renderAnnotationsForPage(pageNum) {
  const svg = document.querySelector(`.annotation-layer[data-page="${pageNum}"]`);
  if (!svg) return;

  // clear existing rendered annotations
  svg.querySelectorAll('.ann-group').forEach(el => el.remove());

  const wrapper = svg.closest('.page-wrapper');
  const pageAnns = annotations.filter(a => a.page === pageNum);

  pageAnns.forEach(ann => {
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.classList.add('ann-group');
    g.dataset.id = ann.id;

    const isOwn = ann.user_id === currentUser.id;
    const uColor = userColor(ann.user_id);

    if (ann.type === 'highlight' || ann.type === 'underline') {
      const color = ann.data.color || '#FFD700';
      ann.data.rects.forEach(nr => {
        const px = toPixels(nr, wrapper);
        if (ann.type === 'highlight') {
          const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
          rect.setAttribute('x', px.x);
          rect.setAttribute('y', px.y);
          rect.setAttribute('width',  px.w);
          rect.setAttribute('height', px.h);
          rect.setAttribute('fill', color);
          rect.setAttribute('fill-opacity', '0.35');
          rect.setAttribute('rx', '2');
          g.appendChild(rect);
        } else {
          // underline: a line at the bottom of the rect
          const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
          line.setAttribute('x1', px.x);
          line.setAttribute('x2', px.x + px.w);
          line.setAttribute('y1', px.y + px.h - 1);
          line.setAttribute('y2', px.y + px.h - 1);
          line.setAttribute('stroke', color);
          line.setAttribute('stroke-width', '2');
          g.appendChild(line);
        }
      });

      // attribution border on left
      const firstPx = toPixels(ann.data.rects[0], wrapper);
      const badge = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      badge.setAttribute('x', firstPx.x - 3);
      badge.setAttribute('y', firstPx.y);
      badge.setAttribute('width', 3);
      badge.setAttribute('height', firstPx.h);
      badge.setAttribute('fill', uColor);
      badge.setAttribute('rx', 1);
      g.appendChild(badge);

    } else if (ann.type === 'note') {
      const px = {
        x: ann.data.x * wrapper.clientWidth,
        y: ann.data.y * wrapper.clientHeight,
      };
      // Note pin: colored circle with initial
      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', px.x);
      circle.setAttribute('cy', px.y);
      circle.setAttribute('r', 12);
      circle.setAttribute('fill', uColor);
      circle.setAttribute('fill-opacity', '0.9');
      g.appendChild(circle);

      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', px.x);
      text.setAttribute('y', px.y + 4);
      text.setAttribute('text-anchor', 'middle');
      text.setAttribute('fill', '#fff');
      text.setAttribute('font-size', '11');
      text.setAttribute('font-weight', 'bold');
      text.setAttribute('font-family', 'sans-serif');
      text.setAttribute('pointer-events', 'none');
      text.textContent = ann.display_name.charAt(0).toUpperCase();
      g.appendChild(text);
    }

    // erase click
    g.addEventListener('click', async (e) => {
      if (currentTool !== 'erase') return;
      e.stopPropagation();
      const canDelete = ann.user_id === currentUser.id || currentUser.role === 'instructor';
      if (!canDelete) { showToast("You can only delete your own annotations."); return; }
      if (!confirm('Delete this annotation?')) return;
      await deleteAnnotation(ann.id);
    });

    // hover tooltip
    g.addEventListener('mouseenter', (e) => showAnnotationTooltip(e, ann));
    g.addEventListener('mouseleave', hideAnnotationTooltip);

    svg.appendChild(g);
  });
}

function renderAllAnnotations() {
  if (!pdfDoc) return;
  for (let p = 1; p <= pdfDoc.numPages; p++) {
    renderAnnotationsForPage(p);
  }
}

// ── Tooltip ────────────────────────────────────────────────────────────────────
let tooltip = null;

function showAnnotationTooltip(e, ann) {
  hideAnnotationTooltip();
  tooltip = document.createElement('div');
  tooltip.style.cssText = `
    position:fixed; z-index:100; background:#1A1A2E; color:#fff;
    padding:6px 10px; border-radius:6px; font-size:12px;
    pointer-events:none; max-width:220px; line-height:1.4;
    box-shadow:0 2px 8px rgba(0,0,0,0.25);
  `;
  let html = `<strong style="color:${userColor(ann.user_id)}">${escHtml(ann.display_name)}</strong>`;
  if (ann.type === 'note') {
    html += `<br>${escHtml(ann.data.text)}`;
  } else if (ann.data.selectedText) {
    html += `<br><em style="opacity:0.75">"${escHtml(ann.data.selectedText.slice(0, 80))}"</em>`;
  }
  tooltip.innerHTML = html;
  document.body.appendChild(tooltip);
  positionTooltip(e);
}

function positionTooltip(e) {
  if (!tooltip) return;
  const pad = 12;
  const tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
  let left = e.clientX + pad, top = e.clientY + pad;
  if (left + tw > window.innerWidth - 8)  left = e.clientX - tw - pad;
  if (top  + th > window.innerHeight - 8) top  = e.clientY - th - pad;
  tooltip.style.left = left + 'px';
  tooltip.style.top  = top  + 'px';
}

function hideAnnotationTooltip() {
  if (tooltip) { tooltip.remove(); tooltip = null; }
}

document.addEventListener('mousemove', e => { if (tooltip) positionTooltip(e); });

// ── Sidebar ────────────────────────────────────────────────────────────────────
function renderSidebar() {
  const list  = document.getElementById('sidebar-list');
  const count = document.getElementById('sidebar-count');
  count.textContent = annotations.length;

  if (!annotations.length) {
    list.innerHTML = '<p style="color:var(--muted);font-size:13px;padding:16px">No annotations yet.<br>Use the tools above to annotate.</p>';
    return;
  }

  list.innerHTML = '';
  // Sort by page then time
  const sorted = [...annotations].sort((a, b) => a.page - b.page || a.created_at.localeCompare(b.created_at));
  sorted.forEach(ann => {
    const isOwn = ann.user_id === currentUser.id;
    const item  = document.createElement('div');
    item.className = `ann-item ${isOwn ? 'mine' : ''}`;
    item.dataset.id = ann.id;

    const typeLabel = { highlight: 'Highlight', underline: 'Underline', note: 'Note' }[ann.type] || ann.type;
    const previewText = ann.type === 'note'
      ? ann.data.text
      : (ann.data.selectedText || '');

    const canDelete = isOwn || currentUser.role === 'instructor';

    item.innerHTML = `
      <div class="ann-item-header">
        <span class="ann-user">
          <span class="dot" style="background:${userColor(ann.user_id)}"></span>
          ${escHtml(ann.display_name)}
        </span>
        <span style="display:flex;align-items:center;gap:6px">
          <span class="ann-type-badge ${ann.type}">${typeLabel}</span>
          <span class="ann-page">p.${ann.page}</span>
          ${canDelete ? `<button class="ann-delete-btn" data-id="${ann.id}" title="Delete">✕</button>` : ''}
        </span>
      </div>
      ${previewText ? `<div class="ann-text">${escHtml(previewText)}</div>` : ''}
    `;

    // click → scroll to page
    item.addEventListener('click', (e) => {
      if (e.target.classList.contains('ann-delete-btn')) return;
      const wrapper = document.querySelector(`.page-wrapper[data-page="${ann.page}"]`);
      if (wrapper) wrapper.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });

    list.appendChild(item);
  });

  list.querySelectorAll('.ann-delete-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!confirm('Delete this annotation?')) return;
      await deleteAnnotation(btn.dataset.id);
    });
  });
}

// ── API calls ──────────────────────────────────────────────────────────────────
async function loadAnnotations() {
  try {
    annotations = await API.get(`/api/annotations/${articleId}`);
    renderAllAnnotations();
    renderSidebar();
  } catch (err) {
    console.error('Failed to load annotations:', err);
  }
}

async function saveAnnotation(type, page, data) {
  try {
    const ann = await API.post('/api/annotations', { article_id: articleId, type, page, data });
    annotations.push(ann);
    renderAnnotationsForPage(page);
    renderSidebar();
  } catch (err) {
    showToast('Failed to save annotation: ' + err.message);
  }
}

async function deleteAnnotation(annId) {
  try {
    await API.del(`/api/annotations/${annId}`);
    const ann = annotations.find(a => a.id === annId);
    annotations = annotations.filter(a => a.id !== annId);
    if (ann) renderAnnotationsForPage(ann.page);
    renderSidebar();
    showToast('Annotation deleted.');
  } catch (err) {
    showToast('Failed to delete: ' + err.message);
  }
}

// ── WebSocket (real-time) ──────────────────────────────────────────────────────
function connectWebSocket() {
  const token  = localStorage.getItem('token');
  const proto  = location.protocol === 'https:' ? 'wss' : 'ws';
  const wsUrl  = `${proto}://${location.host}/ws/${articleId}?token=${token}`;

  ws = new WebSocket(wsUrl);

  ws.addEventListener('message', (e) => {
    const msg = JSON.parse(e.data);

    if (msg.event === 'annotation_added') {
      const ann = msg.annotation;
      // Ignore if it's our own (already added optimistically)
      if (!annotations.find(a => a.id === ann.id)) {
        annotations.push(ann);
        renderAnnotationsForPage(ann.page);
        renderSidebar();
        showToast(`${ann.display_name} added a ${ann.type}`);
      }
    }

    if (msg.event === 'annotation_deleted') {
      const id  = msg.annotation_id;
      const ann = annotations.find(a => a.id === id);
      if (ann) {
        annotations = annotations.filter(a => a.id !== id);
        renderAnnotationsForPage(ann.page);
        renderSidebar();
      }
    }
  });

  ws.addEventListener('close', () => {
    // Reconnect after 3 s if closed unexpectedly
    setTimeout(connectWebSocket, 3000);
  });
}

// ── Init ───────────────────────────────────────────────────────────────────────
async function init() {
  // Load article title
  try {
    const articles = await API.get('/api/articles');
    const article  = articles.find(a => a.id === articleId);
    if (article) document.getElementById('article-title').textContent = article.title;
    document.title = `${article?.title || 'Article'} — Social Reader`;
  } catch (_) {}

  await loadPDF();
  await loadAnnotations();
  connectWebSocket();
}

function escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

init();
