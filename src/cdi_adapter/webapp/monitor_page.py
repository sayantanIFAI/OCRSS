"""``GET /monitor`` — the Pipeline Monitor microsite (owner request 2026-09-26): its own full-screen
app, not a page of the staff site. n8n-style canvas: each stage a node, curved connectors carrying
what flowed between them, the clicked node's details in a side panel. Opened from the main site's
header (new tab), same sign-in. Vanilla JS, inline assets, no CDN (ARCHITECTURE §7)."""

from __future__ import annotations

from cdi_adapter.webapp.theme import LOGO_SVG

CSS = """
:root{--bg:#0b1020;--panel:#111a2e;--panel2:#16213a;--line:#26324a;--text:#e6ecf7;--muted:#8fa1c0;
--brand:#3b82f6;--ok:#22c55e;--run:#f5b301;--bad:#ef4444;--ready:#60a5fa;--font:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--font);
display:grid;grid-template-rows:auto 1fr;overflow:hidden}
a{color:#93c5fd;text-decoration:none}button,input,select{font:inherit;color:var(--text);background:var(--panel2);
border:1px solid var(--line);border-radius:8px;padding:5px 10px}button{cursor:pointer}button:hover{border-color:#3b4a6b}
.top{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
.top .brand{display:flex;align-items:center;gap:8px;font-weight:700}.top .brand small{color:var(--muted);font-weight:400}
.live{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px}
.live i{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 0 rgba(34,197,94,.6);animation:beat 2s infinite}
@keyframes beat{70%{box-shadow:0 0 0 8px rgba(34,197,94,0)}}
.stats{display:flex;gap:8px;margin-left:8px}.stat{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:2px 10px;font-size:12px}
.stat b{font-size:14px;margin-right:4px}.grow{flex:1}
.app{display:grid;grid-template-columns:300px minmax(0,1fr) 440px;min-height:0}
.app.nodetail{grid-template-columns:300px minmax(0,1fr) 0}
.side{border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;min-height:0}
.side .tools{padding:10px;display:grid;gap:8px;border-bottom:1px solid var(--line)}
.list{overflow:auto;flex:1}.item{padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer}
.item:hover{background:var(--panel2)}.item.sel{background:#1c2b4d;box-shadow:inset 3px 0 0 var(--brand)}
.item .t{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.item .m{color:var(--muted);font-size:12px}
.dots{display:flex;gap:3px;margin-top:6px}.dot{width:16px;height:5px;border-radius:3px;background:#2a3550}
.ok{--c:var(--ok)}.running{--c:var(--run)}.failed{--c:var(--bad)}.ready{--c:var(--ready)}
.dot.ok,.dot.running,.dot.failed,.dot.ready{background:var(--c)}.dot.running{animation:pulse 1s infinite}
@keyframes pulse{50%{opacity:.3}}
.pill{display:inline-block;padding:0 8px;border-radius:999px;font-size:11px;border:1px solid var(--c,#3b4a6b);color:var(--c,var(--muted))}
.stage{position:relative;overflow:hidden;cursor:grab;background:var(--bg);
background-image:radial-gradient(#1f2a44 1.2px,transparent 1.2px);background-size:22px 22px}
.stage.drag{cursor:grabbing}.world{position:absolute;left:0;top:0;transform-origin:0 0}
.hint{position:absolute;left:16px;right:16px;top:12px;color:var(--muted);font-size:13px}
.title{position:absolute;left:16px;top:12px;right:16px;display:flex;gap:10px;align-items:center;pointer-events:none}
.title b{font-size:15px;white-space:nowrap}.title span{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.zoom{position:absolute;right:14px;bottom:14px;display:flex;gap:6px}
svg.wires{position:absolute;left:0;top:0;overflow:visible}
svg.wires path{fill:none;stroke:#3b4a6b;stroke-width:2.5}svg.wires path.ok{stroke:var(--ok)}
svg.wires path.running{stroke:var(--run);stroke-dasharray:8 7;animation:dash .7s linear infinite}
@keyframes dash{to{stroke-dashoffset:-15}}svg.wires text{fill:var(--muted);font-size:11px}
.node{position:absolute;width:96px;text-align:center;cursor:pointer;user-select:none}
.node .box{width:96px;height:96px;border-radius:18px;background:var(--panel2);border:2px solid #34405c;display:grid;place-items:center;
transition:transform .12s,box-shadow .12s;position:relative}
.node:hover .box{transform:translateY(-2px)}.node.sel .box{box-shadow:0 0 0 4px rgba(59,130,246,.55)}
.node.ok .box,.node.running .box,.node.failed .box,.node.ready .box{border-color:var(--c)}
.node.ready .box{border-style:dashed}.node.pending{opacity:.45}
.node .ic{font-weight:800;font-size:22px;color:var(--c,#9fb0cc)}.node.running .ic{animation:pulse 1s infinite}
.node .badge{position:absolute;right:-8px;top:-8px;min-width:22px;height:22px;border-radius:11px;background:var(--c,#34405c);
color:#0b1020;font-size:11px;font-weight:700;display:grid;place-items:center;padding:0 5px}
.node .lb{margin-top:8px;font-weight:600;font-size:13px}.node .sub{color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dock{position:absolute;left:0;right:0;top:0;height:0}
.panel{border-left:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;min-height:0;overflow:hidden}
.panel header{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--line)}
.panel header h2{font-size:15px;margin:0}.panel .tabs{display:flex;gap:4px;padding:8px 14px 0}
.tabs button{border-radius:8px 8px 0 0;border-bottom:none}.tabs button.on{background:#1c2b4d;border-color:#3b4a6b}
.body{overflow:auto;padding:12px 14px;flex:1;border-top:1px solid var(--line)}
.kv{display:grid;grid-template-columns:150px 1fr;gap:4px 10px;margin:6px 0 12px}.kv div:nth-child(odd){color:var(--muted)}
.kv div{word-break:break-word}pre{background:#0b1020;border:1px solid var(--line);border-radius:8px;padding:10px;font-size:12px;
white-space:pre-wrap;word-break:break-word;margin:6px 0 12px}h4{margin:12px 0 4px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}tr.low td{background:rgba(245,179,1,.12)}td.n{text-align:right;font-variant-numeric:tabular-nums}
.empty{color:var(--muted);padding:14px}
@media (max-width:1100px){body{display:block;overflow-y:auto;overflow-x:hidden}.stats{display:none}
.app,.app.nodetail{grid-template-columns:1fr;grid-template-rows:auto 60vh auto}.list{max-height:32vh}
.panel{border-left:none;border-top:1px solid var(--line)}.panel .body{max-height:70vh}}
"""

BODY = f"""
<header class=top>
  <div class=brand>{LOGO_SVG}<span>CareFlow <small>· Pipeline Monitor</small></span></div>
  <div class=live><i></i><span id=updated>connecting…</span></div>
  <div class=stats id=stats></div>
  <div class=grow></div>
  <a href="/" title="Back to the CareFlow staff site">← CareFlow</a>
  <form method=post action="/logout" style="margin:0"><button type=submit>Sign out</button></form>
</header>
<div class="app nodetail" id=app>
  <aside class=side>
    <div class=tools>
      <input id=q placeholder="Search file, type, patient…" aria-label=search>
      <select id=filter aria-label="status filter"><option value="">All documents</option><option value=running>Running</option>
        <option value=error>Failed</option><option value=done>Done</option></select>
    </div>
    <div class=list id=items><div class=empty>Loading…</div></div>
  </aside>
  <section class=stage id=stage>
    <div class=world id=world></div>
    <div class=hint id=hint>Select a document on the left to see its flow. Drag to pan, scroll to zoom.</div>
    <div class=zoom><button id=zin title="Zoom in">+</button><button id=zout title="Zoom out">−</button><button id=zfit title="Fit">Fit</button></div>
  </section>
  <aside class=panel id=panel></aside>
</div>"""

SCRIPT = r"""
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const STAGES = {ingest:['Ingest','⤓'], ocr:['OCR','Aa'], classify:['Classify','#'], extract:['AI extract','AI'],
  terminology:['Terminology','⌘'], validate:['Validate','✓'], agent:['Med agent','Rx'], review:['Review','◉'], fhir:['FHIR','{}']};
const W = 96, GAP = 150, Y = 150, X0 = 60;
let items = [], selDoc = null, selStage = null, flow = null, flowTimer = null, tab = 'summary';
let view = {x: 0, y: 0, k: 1};

const when = t => t ? new Date(t).toLocaleTimeString() : '';
const kv = o => '<div class=kv>' + Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== '')
  .map(([k, v]) => `<div>${esc(k)}</div><div>${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</div>`).join('') + '</div>';
const pre = v => `<pre>${esc(JSON.stringify(v, null, 2))}</pre>`;
const table = (rows, cols, rowCls) => !rows || !rows.length ? '<div class=empty>None.</div>' :
  '<table><tr>' + cols.map(c => `<th>${esc(c[1])}</th>`).join('') + '</tr>' + rows.map(r => `<tr class="${rowCls ? rowCls(r) : ''}">` +
  cols.map(c => `<td class="${c[2] || ''}">${esc(r[c[0]] ?? '')}</td>`).join('') + '</tr>').join('') + '</table>';

/* ---------------------------------------------------------------- document list */
async function loadItems(){
  try { const r = await fetch('/api/monitor/items?limit=60'); if (!r.ok) throw 0; items = await r.json(); }
  catch (e) { $('#updated').textContent = 'offline — retrying'; return; }
  $('#updated').textContent = 'live · ' + new Date().toLocaleTimeString();
  const n = s => items.filter(i => i.status === s).length;
  $('#stats').innerHTML = `<span class=stat><b>${items.length}</b>documents</span><span class="stat running"><b>${n('running') + n('pending')}</b>in progress</span>` +
    `<span class="stat failed"><b>${n('error')}</b>failed</span><span class="stat ok"><b>${n('done')}</b>done</span>`;
  renderItems();
}
function renderItems(){
  const q = $('#q').value.toLowerCase(), f = $('#filter').value;
  const shown = items.filter(i => (!f || i.status === f || (f === 'running' && i.status === 'pending')) &&
    (!q || [i.filename, i.doc_type, i.patient, i.patient_name].join(' ').toLowerCase().includes(q)));
  $('#items').innerHTML = shown.length ? shown.map(i => {
    const cls = i.status === 'error' ? 'failed' : i.status === 'done' ? 'ok' : 'running';
    return `<div class="item ${i.document_id && i.document_id === selDoc ? 'sel' : ''}" data-doc="${esc(i.document_id || '')}">
      <div class=t>${esc(i.filename)}</div>
      <div class=m>${esc(when(i.created_at))} · ${esc(i.doc_type || '…')} · <span class="pill ${cls}">${esc(i.status)}</span>${i.facts ? ' · ' + i.facts + ' facts' : ''}${i.patient ? ' · ' + esc(i.patient) : ''}</div>
      <div class=dots>${Object.keys(STAGES).slice(0, 8).map(s => `<span class="dot ${esc(i.stages[s] || '')}" title="${esc(STAGES[s][0])}: ${esc(i.stages[s] || 'pending')}"></span>`).join('')}</div>
      ${i.error ? `<div class=m style="color:var(--bad)">${esc(i.error)}</div>` : ''}</div>`;
  }).join('') : '<div class=empty>No documents match.</div>';
  document.querySelectorAll('.item').forEach(el => el.onclick = () => {
    if (!el.dataset.doc){ $('#hint').textContent = 'This upload has not been stored yet; its flow appears once ingest finishes.'; return; }
    selDoc = el.dataset.doc; selStage = null; renderItems(); loadFlow(true);
  });
}

/* ---------------------------------------------------------------- the flow canvas */
function badge(st){
  const d = st.detail || {}, m = (st.run || {}).metrics || {};
  return {ingest: d.pages && d.pages.length, ocr: d.blocks, classify: null, extract: d.facts, terminology: d.coded,
          validate: m.in_review, review: d.elements ? Object.values(d.elements).reduce((a, b) => a + b, 0) : null, fhir: null}[st.key];
}
function sub(st){
  const r = st.run || {}, d = st.detail || {}, m = r.metrics || {};
  const t = r.seconds != null ? ' · ' + r.seconds + 's' : '';
  switch (st.key){
    case 'ingest': return (d.pages ? d.pages.length + ' page(s)' : '') + t;
    case 'ocr': return (d.blocks != null ? d.blocks + ' lines' : '') + t;
    case 'classify': return (d.doc_type || '') + t;
    case 'extract': return (r.model ? r.model.replace(/^.*\//, '').replace('-Instruct', '') : '') + t;
    case 'terminology': return (d.total ? d.coded + '/' + d.total + ' coded' : '') + t;
    case 'validate': return m.facts != null ? (m.auto_accepted || 0) + ' auto · ' + (m.in_review || 0) + ' held' : st.state;
    case 'agent': return m.lines != null ? (m.suggested || 0) + ' suggested' + t : st.state;
    case 'review': return d.status || st.state;
    case 'fhir': return d.artifact_type || (st.state === 'ready' ? 'ready to build' : st.state);
  }
  return st.state;
}
function wireLabel(st){  // what flowed out of a node, like n8n's "n items"
  const d = st.detail || {};
  return {ingest: d.pages ? d.pages.length + ' page' + (d.pages.length === 1 ? '' : 's') : '', ocr: d.blocks != null ? d.blocks + ' lines' : '',
          classify: d.doc_type || '', extract: d.facts != null ? d.facts + ' facts' : '', terminology: d.total ? d.total + ' facts' : '',
          validate: st.state === 'ok' ? 'checked' : '', agent: d.lines ? d.lines.length + ' medicines' : '', review: d.status || ''}[st.key] || '';
}
function renderFlow(){
  const st = flow.stages;
  $('#hint').innerHTML = `<div class=title><b>${esc(flow.document.filename)}</b><span>document ${esc(flow.document.id.slice(0, 8))} · ${esc(flow.document.status)}${flow.job ? ' · job ' + esc(flow.job.state) : ''}</span></div>`;
  const pos = st.map((s, i) => ({x: X0 + i * GAP, y: Y}));
  let wires = '';
  st.forEach((s, i) => {
    if (i === st.length - 1) return;
    const a = pos[i], b = pos[i + 1], x1 = a.x + W, y1 = a.y + W / 2, x2 = b.x - 6, y2 = b.y + W / 2, mx = (x1 + x2) / 2;
    const cls = st[i + 1].state === 'running' ? 'running' : (s.state === 'ok' ? 'ok' : '');
    wires += `<path class="${cls}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}" marker-end="url(#arr${cls})"/>`;
    const lab = s.state === 'ok' ? wireLabel(s) : '';
    if (lab) wires += `<text x="${mx}" y="${y1 - 10}" text-anchor="middle">${esc(lab)}</text>`;
  });
  const mk = (id, c) => `<marker id="arr${id}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="${c}" stroke="none"/></marker>`;
  const svg = `<svg class=wires width="${X0 * 2 + st.length * GAP}" height="${Y * 2 + W}"><defs>${mk('', '#3b4a6b')}${mk('ok', '#22c55e')}${mk('running', '#f5b301')}</defs>${wires}</svg>`;
  const nodes = st.map((s, i) => { const b = badge(s);
    return `<div class="node ${esc(s.state)} ${s.key === selStage ? 'sel' : ''}" data-k="${s.key}" style="left:${pos[i].x}px;top:${pos[i].y}px" title="${esc(STAGES[s.key][0])}: ${esc(s.state)}">
      <div class=box><span class=ic>${esc(STAGES[s.key][1])}</span>${b != null && b !== '' ? `<span class=badge>${esc(b)}</span>` : ''}</div>
      <div class=lb>${esc(STAGES[s.key][0])}</div><div class=sub>${esc(sub(s))}</div></div>`; }).join('');
  $('#world').innerHTML = svg + nodes;
  document.querySelectorAll('.node').forEach(el => el.onclick = e => { e.stopPropagation(); selStage = el.dataset.k; renderFlow(); renderPanel(); });
  applyView();
}
async function loadFlow(fit){
  clearTimeout(flowTimer);
  if (!selDoc) return;
  const r = await fetch('/api/monitor/documents/' + encodeURIComponent(selDoc));
  if (!r.ok){ $('#hint').textContent = 'Could not load this document (' + r.status + ').'; return; }
  flow = await r.json();
  renderFlow(); if (fit) fitView(); if (selStage) renderPanel();
  if (flow.stages.some(s => s.state === 'running') || (flow.job && flow.job.state === 'running')) flowTimer = setTimeout(() => loadFlow(false), 2000);
}

/* pan + zoom */
function applyView(){ $('#world').style.transform = `translate(${view.x}px,${view.y}px) scale(${view.k})`; }
function fitView(){
  const s = $('#stage').getBoundingClientRect(), w = X0 * 2 + (flow ? flow.stages.length : 8) * GAP - (GAP - W), h = W + 60;
  view.k = Math.max(0.4, Math.min(1.3, (s.width - 40) / w)); view.x = (s.width - w * view.k) / 2; view.y = (s.height - h * view.k) / 2 - Y * view.k + 20; applyView();
}
(() => { const stage = $('#stage'); let drag = null;
  stage.onmousedown = e => { if (e.target.closest('.node,.zoom')) return; drag = {x: e.clientX - view.x, y: e.clientY - view.y}; stage.classList.add('drag'); };
  window.onmousemove = e => { if (!drag) return; view.x = e.clientX - drag.x; view.y = e.clientY - drag.y; applyView(); };
  window.onmouseup = () => { drag = null; stage.classList.remove('drag'); };
  stage.onwheel = e => { e.preventDefault(); const r = stage.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top, k = Math.max(0.3, Math.min(2.5, view.k * (e.deltaY < 0 ? 1.1 : 0.9)));
    view.x = px - (px - view.x) * k / view.k; view.y = py - (py - view.y) * k / view.k; view.k = k; applyView(); };
  $('#zin').onclick = () => { view.k = Math.min(2.5, view.k * 1.2); applyView(); };
  $('#zout').onclick = () => { view.k = Math.max(0.3, view.k / 1.2); applyView(); };
  $('#zfit').onclick = fitView; })();

/* ---------------------------------------------------------------- details panel (n8n style, right) */
function renderPanel(){
  const s = flow && flow.stages.find(x => x.key === selStage);
  if (!s){ $('#app').classList.add('nodetail'); return; }
  $('#app').classList.remove('nodetail');
  const cls = s.state === 'ok' ? 'ok' : s.state === 'failed' ? 'failed' : s.state === 'running' ? 'running' : s.state === 'ready' ? 'ready' : '';
  $('#panel').innerHTML = `<header><span style="font-size:18px">${esc(STAGES[s.key][1])}</span><h2>${esc(STAGES[s.key][0])}</h2><span class="pill ${cls}">${esc(s.state)}</span>
      <span class=grow></span><button id=pclose title="Close">✕</button></header>
    <div class=tabs><button data-t=summary class="${tab === 'summary' ? 'on' : ''}">Summary</button><button data-t=data class="${tab === 'data' ? 'on' : ''}">Data (JSON)</button></div>
    <div class=body id=pbody></div>`;
  $('#pclose').onclick = () => { selStage = null; $('#app').classList.add('nodetail'); renderFlow(); };
  document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => { tab = b.dataset.t; renderPanel(); });
  $('#pbody').innerHTML = tab === 'data' ? pre({state: s.state, run: s.run, detail: s.detail}) : summary(s);
  const fb = $('#fhirb'); if (fb) fb.onclick = buildFhir;
}
function summary(s){
  const r = s.run, d = s.detail;
  let h = r ? '<h4>Run</h4>' + kv({engine: r.model, 'engine version': r.model_version, started: when(r.started_at), finished: when(r.finished_at),
    'time (s)': r.seconds, 'runs of this step': r.attempts, error: r.error}) : '';
  if (!d && s.key !== 'fhir') return h + '<div class=empty>Nothing recorded for this step yet.</div>';
  switch (s.key){
    case 'ingest': return h + '<h4>File</h4>' + kv({file: d.filename, type: d.mime_type, bytes: d.bytes, sha256: d.sha256, channel: d.channel,
      'document status': d.document_status, 'document error': d.document_error}) + '<h4>Pages</h4>' +
      table(d.pages, [['page_no', 'Page', 'n'], ['width', 'Width', 'n'], ['height', 'Height', 'n'], ['dpi', 'DPI', 'n']]);
    case 'ocr': return h + '<h4>Output</h4>' + kv({'lines read': d.blocks, engines: (d.engines || []).join(', '), 'mean confidence': d.mean_confidence,
      'low-confidence lines': d.low_confidence_blocks}) + `<h4>Lines (${d.lines_shown} of ${d.blocks}, low confidence highlighted)</h4>` +
      table(d.lines, [['page_no', 'Pg', 'n'], ['reading_order', '#', 'n'], ['text', 'Text'], ['ocr_conf', 'Conf', 'n']], x => x.ocr_conf != null && x.ocr_conf < 0.6 ? 'low' : '');
    case 'classify': return h + '<h4>Result</h4>' + kv({'document type': d.doc_type, confidence: d.confidence,
      method: d.method === 'vlm' ? 'AI model' : 'rules (fast path)', handwritten: d.is_handwritten, specialty: d.specialty, languages: (d.languages || []).join(', ')});
    case 'extract': return h + '<h4>Result</h4>' + kv({model: d.model_name, schema: d.schema_id, 'partial answer': d.is_partial, facts: d.facts}) +
      '<h4>What the model returned</h4>' + pre(d.payload);
    case 'terminology': return h + '<h4>Coding</h4>' + kv({'facts coded': d.coded + ' of ' + d.total}) +
      table(d.facts, [['fact_type', 'Type'], ['local_text', 'As written'], ['code', 'Code'], ['code_display', 'Display'], ['code_status', 'Mapping'], ['catalogue', 'Catalogue (Tata 1mg)'], ['review_state', 'Review']]);
    case 'validate': return h + '<h4>Gate</h4>' + kv({facts: d.facts, 'auto-accepted': d.auto_accepted, 'held for review': d.in_review}) +
      '<div class=empty>Confident, consistent facts are auto-accepted; the rest are held for a clinician.</div>';
    case 'agent': return h + '<h4>Suggestions (advisory: a reviewer applies them)</h4>' +
      table(d.lines, [['read_as', 'As read'], ['status', 'Agent'], ['product', 'Product'], ['treats', 'Treats'], ['why', 'Because'], ['evidence', 'Written'], ['confidence', 'Conf', 'n'], ['rationale', 'Why']]) +
      '<h4>Steps (agent trace)</h4>' + table(d.trace, [['seq', '#', 'n'], ['step', 'Step'], ['tool', 'Tool'], ['duration_ms', 'ms', 'n'], ['confidence', 'Conf', 'n']]);
    case 'review': return '<h4>Review item</h4>' + kv({status: d.status, 'claimed by': d.assigned_to, claimed: when(d.claimed_at), decided: when(d.decided_at)}) +
      '<h4>Elements by state</h4>' + kv(d.elements || {}) + '<a href="/reviewer" target=_blank rel=noopener>Open in the Reviewer ↗</a>';
    case 'fhir': return (d ? '<h4>Stored bundle</h4>' + kv({artifact: d.artifact_type, validation: d.validation_status, status: d.status, FHIR: d.fhir_version, IG: d.ig_package, created: when(d.created_at)})
      : '<div class=empty>No stored bundle yet: it is stored when the review item is approved.</div>') + '<button id=fhirb>Build a FHIR preview now</button><div id=fhirout></div>';
  }
  return h;
}
async function buildFhir(){
  $('#fhirout').innerHTML = '<div class=empty>Building…</div>';
  const r = await fetch('/api/documents/' + encodeURIComponent(selDoc) + '/bundle');
  if (!r.ok){ $('#fhirout').innerHTML = `<div class=empty style="color:var(--bad)">Could not build (${r.status}).</div>`; return; }
  const x = await r.json(), res = ((x.bundle || {}).entry || []).map(e => e.resource.resourceType), counts = {};
  res.forEach(t => counts[t] = (counts[t] || 0) + 1);
  $('#fhirout').innerHTML = '<h4>Preview</h4>' + kv({artifact: x.artifact_type, status: x.status, validation: x.validation_status, issues: (x.issues || []).length,
    resources: res.length, 'facts included': (x.asserted_facts || []).length, 'facts still held': (x.held_facts || []).length}) + '<h4>Resources</h4>' + kv(counts) +
    ((x.issues || []).length ? '<h4>Issues</h4>' + pre(x.issues) : '');
}

$('#q').oninput = renderItems; $('#filter').onchange = renderItems;
window.onresize = () => flow && fitView();
loadItems(); setInterval(loadItems, 3000);
"""


def monitor_page() -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>Pipeline Monitor — CareFlow</title><style>{CSS}</style></head>"
        f"<body>{BODY}<script>{SCRIPT}</script></body></html>"
    )
