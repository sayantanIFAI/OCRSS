"""``GET /reviewer`` — workbasket -> worklist console (ARCHITECTURE §4.5). Vanilla JS, no CDN."""

from __future__ import annotations

from cdi_adapter.webapp.theme import page

BODY = """
<style>
.rv-split{display:grid;grid-template-columns:minmax(0,1fr) minmax(320px,44%);gap:20px;align-items:start}
.rv-doc{position:sticky;top:12px;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface);
  display:flex;flex-direction:column;max-height:calc(100vh - 24px)}
.rv-doc .bar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:8px;border-bottom:1px solid var(--border)}
.rv-doc .bar button{padding:2px 9px}.rv-doc .view{overflow:auto;padding:8px;flex:1;background:#eef2f9}
.rv-doc img{display:block;margin:0 auto;box-shadow:0 1px 4px rgba(15,27,51,.25);background:#fff}
.rv-doc [hidden]{display:none}
@media (max-width:1000px){.rv-split{grid-template-columns:1fr}.rv-doc{position:static;max-height:80vh}}
</style>
<h1 style="margin:0 0 4px">Reviewer</h1>
<p class=muted>Claim an item into your worklist, decide its held facts, then approve to promote it into the canonical EMR and generate its FHIR bundle.</p>
<p>Signed in as <b id=me>…</b> <button id=load class=primary>Refresh</button></p>
<div class=grid style="grid-template-columns:1fr 1fr">
  <section class=card><h2 style="margin-top:0">Workbasket</h2><div id=wb class=muted>—</div></section>
  <section class=card><h2 style="margin-top:0">My worklist</h2><div id=wl class=muted>—</div></section>
</div>
<section class=card id=item style="margin-top:16px"><p class=muted>Open an item from your worklist.</p></section>
<section class=card style="margin-top:16px"><h2 style="margin-top:0">Approved</h2>
  <p class=muted>Latest approved items. Download each one's FHIR bundle (built from the canonical EMR).</p><div id=ap class=muted>—</div></section>
"""

SCRIPT = r"""
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// the reviewer is the signed-in user (backlog E7-S1): the server takes it from the session
fetch('/api/me').then(r => r.json()).then(u => { $('#me').textContent = u.display_name + ' (' + u.username + ')'; });
const post = (url, body) => fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
// Customer 360 (backlog E7-S8): the patient's canonical record across documents
const c360 = mpi => mpi ? ` <a href="/reviewer/c360?mpi=${encodeURIComponent(mpi)}" title="The patient's record across documents">Customer 360</a>` : '';
const row = (i, btn) => `<tr><td>${esc(i.patient_name)} <span class=muted>${esc(i.mpi_id)}</span>${c360(i.mpi_id)}<br><small>${esc(i.filename)} · ${esc(i.doc_type)}</small></td>
  <td>${i.held ? `<span class="pill warn">${i.held} held</span>` : '<span class="pill ok">ready</span>'} ${i.elements} facts</td><td>${btn}</td></tr>`;
async function load(){
  const wb = await (await fetch('/api/reviewer/workbasket')).json();
  $('#wb').innerHTML = wb.length ? '<table>' + wb.map(i => row(i, `<button onclick="claim('${i.id}')">Claim</button>`)).join('') + '</table>' : 'Empty.';
  const wl = await (await fetch('/api/reviewer/worklist')).json();
  $('#wl').innerHTML = wl.length ? '<table>' + wl.map(i => row(i, `<button onclick="openItem('${i.id}')">Open</button>`)).join('') + '</table>' : 'Empty.';
  const ap = await (await fetch('/api/reviewer/approved')).json();
  $('#ap').innerHTML = ap.length ? '<table>' + ap.map(i => `<tr><td>${esc(i.patient_name)} <span class=muted>${esc(i.mpi_id)}</span>${c360(i.mpi_id)}<br><small>${esc(i.filename)} · ${esc(i.doc_type)}</small></td>
    <td>${i.bundle_status === 'ready_to_share' ? '<span class="pill ok">ready to share</span>' : `<span class="pill warn">${esc(i.bundle_status || 'no bundle')}</span>`}</td>
    <td>${i.bundle_status ? `<a href="${download(i.id)}">Download FHIR bundle</a>` : ''}</td></tr>`).join('') + '</table>' : 'None yet.';
}
const download = id => `/api/reviewer/items/${id}/bundle/download`;
async function claim(id){ const r = await post(`/api/reviewer/items/${id}/claim`, {}); if (!r.ok) alert((await r.json()).detail); load(); }
// Edit (2026-09-25): a fact stays correctable until its item is approved; a medication's
// detail is what the canonical EMR and FHIR carry, so its form edits that detail
let current = {};
const num = v => String(Number(v));
const FIELDS = {
  text: [['local_text', 'Text']],
  measured: [['value_text', 'Value (as written)'], ['value_num', 'Value (number)'], ['value_unit_ucum', 'Unit']],
  medication: [['drug_text', 'Medicine'], ['strength_num', 'Strength'], ['strength_unit', 'Strength unit (mg, ml, …)'],
    ['dose_num', 'Dose'], ['dose_unit_ucum', 'Dose unit (tab, ml, …)'], ['frequency_code', 'Frequency (1-0-1, BD, OD, SOS, …)'],
    ['duration_days', 'Duration (days)'], ['route', 'Route'], ['instructions', 'Instructions (after food, …)']],
  // every other detail of a fact is editable too (owner, 2026-09-28: "all the details an option to edit")
  medication_more: [['form', 'Form (tablet, capsule, syrup, …)'], ['prn', 'As needed (SOS)']],
  measured_more: [['abnormal_flag', 'Flag (H, L, N, …)'], ['ref_range_low', 'Reference range low'],
    ['ref_range_high', 'Reference range high'], ['ref_range_text', 'Reference range (as written)']],
  status: [['clinical_status', 'Clinical status (active, resolved, …)']],
  code: [['code_system', 'Code system'], ['code', 'Code'], ['code_display', 'Code display']],
};
const MED_KEYS = new Set(FIELDS.medication.concat(FIELDS.medication_more).map(f => f[0]));
const fieldsFor = e => (e.element_type === 'medication' ? FIELDS.medication.concat(FIELDS.medication_more)
  : FIELDS.text.concat(['vital_sign', 'lab_result'].includes(e.element_type) ? FIELDS.measured.concat(FIELDS.measured_more) : [])
    .concat(['condition', 'symptom', 'allergy', 'finding'].includes(e.element_type) ? FIELDS.status : []))
  .concat(FIELDS.code);
const valueOf = (e, k) => { const src = e.element_type === 'medication' && MED_KEYS.has(k) ? (e.medication || {}) : (e.fact || {});
  const v = src[k] ?? (k === 'drug_text' ? e.fact?.local_text : null); return v == null ? '' : String(v); };
// the input for one field: "as needed" is yes / no, every other field is text
const fieldInput = (e, k) => k === 'prn'
  ? `<select data-k="prn"><option value="false"${valueOf(e, k) === 'true' ? '' : ' selected'}>no</option><option value="true"${valueOf(e, k) === 'true' ? ' selected' : ''}>yes</option></select>`
  : `<input data-k="${k}" value="${esc(valueOf(e, k))}" style="width:100%">`;
function editFact(fid, item){
  const box = document.getElementById('ed-' + fid);
  if (box.style.display === 'table-row'){ box.style.display = 'none'; return; }
  const e = current[fid];
  box.querySelector('td').innerHTML = fieldsFor(e).map(([k, label]) => `<label style="display:block;margin:4px 0"><small>${esc(label)}</small><br>
      ${fieldInput(e, k)}</label>`).join('')
    + `<label style="display:block;margin:4px 0"><small>Reason (optional)</small><br><input data-note style="width:100%"></label>
      <p><button class=primary onclick="saveFact('${fid}','${item}')">Save correction</button> <button onclick="editFact('${fid}','${item}')">Cancel</button> <span data-msg></span></p>`;
  box.style.display = 'table-row';
}
async function saveFact(fid, item){
  const e = current[fid], box = document.getElementById('ed-' + fid), corrections = {};
  box.querySelectorAll('[data-k]').forEach(i => { if (i.value !== valueOf(e, i.dataset.k) && !(i.dataset.k === 'prn' && i.value === 'false' && valueOf(e, 'prn') === ''))
    corrections[i.dataset.k] = i.dataset.k === 'prn' ? i.value === 'true' : i.value; });
  const msg = box.querySelector('[data-msg]');
  if (!Object.keys(corrections).length){ msg.textContent = 'Nothing changed.'; return; }
  const r = await post(`/api/facts/${fid}/review`, {action: 'correct', corrections, note: box.querySelector('input[data-note]').value || null});
  if (!r.ok){ msg.textContent = 'Error: ' + (await r.json()).detail; return; }
  openItem(item); load();
}
// the item's facts grouped into clinical sections, in the order a clinician reads a record
// (owner, 2026-09-26: "the extracted data should be grouped properly"); a section's columns suit
// its kind of fact, and an unknown fact type is never dropped: it lands in "Other"
const SECTIONS = [['condition', 'Diagnoses'], ['symptom', 'Symptoms & complaints'], ['finding', 'Examination findings'],
  ['vital_sign', 'Vitals'], ['lab_result', 'Lab results'], ['diagnostic_report', 'Reports'], ['medication', 'Medications'], ['lab_order', 'Tests advised'],
  ['procedure', 'Procedures'], ['allergy', 'Allergies'], ['immunization', 'Immunizations'], ['care_plan', 'Care plan'],
  ['referral', 'Referrals'], ['advice', 'Advice & follow-up'], ['document', 'Other']];
const unit = (n, u) => n == null ? '' : `${num(n)} ${u || ''}`.trim();
const COLS = {
  medication: [['Medicine', e => (e.medication || {}).drug_text || e.fact?.local_text], ['Strength', e => unit((e.medication || {}).strength_num, (e.medication || {}).strength_unit)],
    ['Dose', e => unit((e.medication || {}).dose_num, (e.medication || {}).dose_unit_ucum)],
    ['Frequency', e => [(e.medication || {}).frequency_code, (e.medication || {}).prn ? 'as needed' : ''].filter(Boolean).join(' · ')],
    ['Duration', e => (e.medication || {}).duration_days != null ? num(e.medication.duration_days) + ' days' : ''],
    ['Route', e => (e.medication || {}).route], ['Instructions', e => (e.medication || {}).instructions],
    ['Catalogue (Tata 1mg)', e => catalogueText(e.catalogue)], ['Agent (diagnosis check)', e => agentText(e.agent)]],
  measured: [['Test', e => e.fact?.local_text], ['Value', e => e.fact?.value_text || (e.fact?.value_num != null ? num(e.fact.value_num) : '')],
    ['Unit', e => e.fact?.value_unit_ucum], ['Flag', e => e.fact?.abnormal_flag]],
  text: [['Detail', e => e.fact?.local_text]],
  order: [['Test (as written)', e => e.fact?.local_text]],
};
// the catalogue's identification of the line (owner, 2026-09-26): the product, or UNKNOWN_MEDICATION
// with the closest catalogue names; the medicine column above stays as written
function catalogueText(c){
  if (!c) return '';
  if (c.status === 'matched') return [c.product_name || c.display_name, c.product_name ? c.composition : '', c.manufacturer,
    c.method === 'reviewer' ? 'chosen by reviewer' : ['exact', 'generic'].includes(c.method) ? 'exact' : 'match ' + Number(c.score).toFixed(2), c.strength_match === false ? 'strength differs' : ''].filter(Boolean).join(' · ');
  const near = (c.candidates || []).slice(0, 3).map(x => x.name).join(', ');  // nearest name, written form and strength first
  return 'UNKNOWN_MEDICATION' + (near ? ` (closest: ${near})` : '');
}
// the medication agent (backlog E12-S4, owner 2026-09-26): re-reads the line with the prescription's
// diagnoses and the catalogue, and SUGGESTS a product; the reviewer applies it (never automatic)
function agentText(a){
  if (!a) return '';
  const fit = a.treats ? 'treats ' + a.treats + (a.why ? ` (${a.why})` : '') : a.fits_diagnosis === false ? '⚠ no matching diagnosis' : '';
  const conf = a.confidence != null ? ` (${Math.round(a.confidence * 100)}%)` : '';
  // the recall step (2026-09-28): what the model's own knowledge names for the line
  const rc = a.recalled || {}, thinks = (rc.brands || []).concat(rc.generics || []);
  if (thinks.length && a.status !== 'confirmed' && a.status !== 'suggested')
    return [a.status === 'vetoed' ? `Unsure: ${a.product ? a.product.name : ''}? (${a.reason})` : 'No confident match',
      `agent thinks: ${thinks.join(', ')}`, rc.reason, 'see Replace'].filter(Boolean).join(' · ');
  if (a.status === 'confirmed') return `✓ ${fit || 'identified'}`;
  if (a.status === 'suggested') return [`Suggests ${a.product.name}${conf}`, fit, a.evidence ? `written "${a.evidence}"` : '', a.rationale].filter(Boolean).join(' · ');
  if (a.status === 'vetoed') return `Unsure: ${a.product ? a.product.name : ''}? (${a.reason})`;
  return 'No confident match';
}
function agentRun(r){
  if (!r) return 'not run yet';
  if (r.status === 'running') return 'running… (about a minute)';
  if (r.status === 'failed') return 'failed: ' + (r.error || '');
  const m = r.metrics || {};
  return `done · ${m.suggested || 0} suggested · ${m.no_matching_diagnosis || 0} without a matching diagnosis`;
}
async function rerunAgent(id){
  const r = await post(`/api/reviewer/items/${id}/agent`, {});
  if (!r.ok){ alert((await r.json()).detail); return; }
  const el = document.getElementById('agentst'); if (el) el.textContent = 'queued…';
  pollAgent(id, 0, Date.now());
}
function pollAgent(id, n, since){  // re-open the item once a run started after `since` has finished
  if (n > 60) return;
  setTimeout(async () => { const d = await (await fetch(`/api/reviewer/items/${id}/elements`)).json();
    const r = d.agent_run, fresh = r && Date.parse(r.started_at) >= since - 5000;
    if (fresh && r.status !== 'running') openItem(id); else pollAgent(id, n + 1, since); }, 5000);
}
function useAgent(fid, item){  // the Edit form, pre-filled with the agent's product; the reviewer saves
  const e = current[fid]; editFact(fid, item);
  const box = document.getElementById('ed-' + fid), i = box && box.querySelector('input[data-k="drug_text"]');
  if (i && e.agent && e.agent.product){ i.value = e.agent.product.name;
    box.querySelector('input[data-note]').value = 'medication agent: ' + (e.agent.rationale || e.agent.evidence || ''); i.focus(); }
}
const colsFor = t => t === 'medication' ? COLS.medication : ['vital_sign', 'lab_result'].includes(t) ? COLS.measured
  : t === 'lab_order' ? COLS.order : COLS.text;
function factRows(e, id, cols){
  const n = cols.length + 3;
  return `<tr>${cols.map(([, f]) => `<td>${esc(f(e) ?? '')}</td>`).join('')}
    <td>${esc(e.fact?.code)}${e.fact?.code_display ? `<br><small class=muted>${esc(e.fact.code_display)}</small>` : ''}</td>
    <td><span class="pill ${e.state==='held'?'warn':e.state==='rejected'?'bad':'ok'}">${esc(e.state)}</span></td>
    <td style="white-space:nowrap">${e.state==='held' ? `<button class=ok onclick="decide('${e.fact.id}','accept','${id}')">Accept</button> <button class=bad onclick="decide('${e.fact.id}','reject','${id}')">Reject</button> ` : ''}${e.fact && e.state !== 'rejected' ? `<button onclick="editFact('${e.fact.id}','${id}')">Edit</button>` : ''}${e.agent && e.agent.status === 'suggested' && e.state !== 'rejected' ? ` <button class=primary onclick="useAgent('${e.fact.id}','${id}')" title="Open the edit form with the agent's product filled in">Use suggestion</button>` : ''}${e.element_type === 'medication' && e.fact && e.state !== 'rejected' ? ` <button onclick="openReplace('${e.fact.id}','${id}')" title="Choose the medicine from the catalogue or the agent's options">Replace</button>` : ''}${e.state==='held' ? ' <a href="/review">details</a>' : ''}</td></tr>
    ${e.fact ? `<tr id="ed-${e.fact.id}" style="display:none"><td colspan=${n} style="background:var(--brand-weak)"></td></tr>` : ''}
    ${replaceRow(e, id, n)}`;
}
// Replace (owner, 2026-09-28): the products the catalogue and the medication agent offer for a
// medicine line, plus a catalogue search; the reviewer picks one and it replaces the medicine
// through the same correction as Edit (dose, frequency and the rest of the line are kept)
function replaceRow(e, item, n){
  if (e.element_type !== 'medication' || !e.fact || e.state === 'rejected') return '';
  return `<tr id="rp-${e.fact.id}" style="display:none"><td colspan=${n} style="background:var(--brand-weak)"></td></tr>`;
}
let replacing = {};
const optLine = (o, list, i) => `<label style="display:block;margin:3px 0;cursor:pointer"><input type=radio name="rp-${replacing.fid}" value="${list}:${i}">
  <b>${esc(o.name)}</b> <small class=muted>${esc([o.why, o.manufacturer, o.composition, o.pack_size_label,
    o.generic ? 'generic' : '', o.discontinued ? 'discontinued' : '', o.strength_match === false ? 'strength differs from the line' : ''].filter(Boolean).join(' · '))}</small></label>`;
function optGroup(title, list, opts, empty){
  return `<p style="margin:8px 0 2px"><b>${esc(title)}</b></p>` + (opts.length ? opts.map((o, i) => optLine(o, list, i)).join('') : `<small class=muted>${esc(empty)}</small>`);
}
async function openReplace(fid, item, q){
  const box = document.getElementById('rp-' + fid);
  if (!box) return;
  if (q === undefined && box.style.display === 'table-row'){ box.style.display = 'none'; return; }
  const ed = document.getElementById('ed-' + fid); if (ed) ed.style.display = 'none';
  box.style.display = 'table-row';
  const cell = box.querySelector('td');
  if (q === undefined) cell.innerHTML = '<small class=muted>Loading the catalogue and agent options…</small>';
  const r = await fetch(`/api/reviewer/facts/${fid}/replacements` + (q ? '?q=' + encodeURIComponent(q) : ''));
  const d = await r.json();
  if (!r.ok){ cell.innerHTML = `<small>Error: ${esc(d.detail)}</small>`; return; }
  replacing = {fid, item, d};
  if (d.catalogue_empty){ cell.innerHTML = '<small class=muted>The medicine catalogue is empty on this server: nothing to choose from.</small>'; return; }
  cell.innerHTML = `<p style="margin:0 0 4px">Replace <b>${esc(d.written)}</b> with:</p>
    ${optGroup('From the catalogue (Tata 1mg)', 'catalogue', d.catalogue, 'No catalogue products are close to this line.')}
    ${optGroup('From the medication agent', 'agent', d.agent, 'The agent has no suggestion for this line (use "Run agent again" to ask it).')}
    <p style="margin:10px 0 2px"><b>Search the catalogue</b></p>
    <input id="rpq-${fid}" value="${esc(q || '')}" placeholder="brand or generic name, e.g. Dolo 650 or Paracetamol" style="width:70%"
      onkeydown="if(event.key==='Enter')openReplace('${fid}','${item}',this.value)"> <button onclick="openReplace('${fid}','${item}',document.getElementById('rpq-${fid}').value)">Search</button>
    ${q ? optGroup('Search results', 'search', d.search, 'Nothing found in the catalogue.') : ''}
    <label style="display:block;margin:8px 0 4px"><small>Reason (optional)</small><br><input data-rpnote style="width:100%"></label>
    <p><button class=primary onclick="saveReplace()">Replace with selected</button> <button onclick="openReplace('${fid}','${item}')">Cancel</button> <span data-rpmsg></span></p>`;
}
async function saveReplace(){
  const {fid, item, d} = replacing, box = document.getElementById('rp-' + fid);
  const msg = box.querySelector('[data-rpmsg]'), picked = box.querySelector(`input[name="rp-${fid}"]:checked`);
  if (!picked){ msg.textContent = 'Select a medicine first.'; return; }
  const [list, i] = picked.value.split(':'), o = d[list][Number(i)];
  msg.textContent = 'Replacing…';
  const r = await post(`/api/reviewer/facts/${fid}/replace`, {catalogue_id: o.id, generic: o.id == null ? o.generic : null,
    source: list, note: box.querySelector('input[data-rpnote]').value || null});
  if (!r.ok){ msg.textContent = 'Error: ' + (await r.json()).detail; return; }
  openItem(item); load();
}
async function openItem(id){
  const d = await (await fetch(`/api/reviewer/items/${id}/elements`)).json();
  current = {};
  d.elements.forEach(e => { if (e.fact) current[e.fact.id] = e; });
  const known = new Set(SECTIONS.map(s => s[0])), groups = {};
  d.elements.forEach(e => { const k = known.has(e.element_type) ? e.element_type : 'document'; (groups[k] = groups[k] || []).push(e); });
  const shown = SECTIONS.filter(([k]) => groups[k]);
  const held = d.elements.filter(e => e.state === 'held').length;
  const chips = shown.map(([k, label]) => `<span class=pill style="margin-right:4px">${esc(label)} ${groups[k].length}</span>`).join('');
  const sections = shown.map(([k, label]) => { const cols = colsFor(k), g = groups[k], h = g.filter(e => e.state === 'held').length;
    return `<h3 style="margin:18px 0 6px">${esc(label)} <span class=muted style="font-weight:400;font-size:14px">${g.length}${h ? ` · <span class="pill warn">${h} held</span>` : ''}</span></h3>
      <div style="overflow-x:auto"><table><tr>${cols.map(([c]) => `<th>${esc(c)}</th>`).join('')}<th>Code</th><th>State</th><th></th></tr>${g.map(e => factRows(e, id, cols)).join('')}</table></div>`; }).join('');
  $('#item').innerHTML = `<div class=rv-split><div>
    <h2 style="margin:0 0 2px">${esc(d.item.patient_name)} <span class=muted style="font-weight:400">${esc(d.item.mpi_id || '')}</span><span style="font-size:14px;font-weight:400">${c360(d.item.mpi_id)}</span></h2>
    <p class=muted style="margin:0 0 8px">${esc(d.item.filename)} · ${esc(d.item.doc_type)} · ${d.elements.length} facts${held ? ` · <span class="pill warn">${held} held</span>` : ''}</p>
    <div>${chips}</div>${groups.medication ? `<p class=muted style="margin:10px 0 0">Medication agent: <span id=agentst>${esc(agentRun(d.agent_run))}</span> <button onclick="rerunAgent('${id}')" title="Re-read the medicines with the diagnoses and the catalogue">Run agent again</button></p>` : ''}${sections || '<p class=muted>No facts.</p>'}
    <p style="margin-top:18px"><button class=primary onclick="approve('${id}')">Approve &amp; promote</button> <button onclick="rejectItem('${id}')">Reject item</button> <span id=msg></span></p><pre id=out style="white-space:pre-wrap"></pre>
    </div>${docViewer(d.item)}</div>`;
  showPage();
  if (d.agent_run && d.agent_run.status === 'running') pollAgent(id, 0, Date.parse(d.agent_run.started_at));
}
// the uploaded document beside its facts (owner, 2026-09-26): the rendered pages the facts were read
// from, page by page, plus the file exactly as uploaded
let viewer = {id: null, pages: 1, page: 1, zoom: 1};
function docViewer(item){
  viewer = {id: item.source_document_id, pages: Math.max(1, item.page_count || 1), page: 1, zoom: 1};
  if (!viewer.id) return '<aside class=rv-doc><div class="view muted">No document is attached to this item.</div></aside>';
  return `<aside class=rv-doc><div class=bar><b style="margin-right:auto">Uploaded document</b>
      <button onclick="turn(-1)" title="Previous page">‹</button><span id=pg class=muted></span><button onclick="turn(1)" title="Next page">›</button>
      <button onclick="zoomBy(1/1.25)" title="Zoom out">−</button><button onclick="zoomBy(1.25)" title="Zoom in">+</button><button onclick="zoomBy(0)" title="Fit width">Fit</button>
      <a href="/api/documents/${esc(viewer.id)}/original" target=_blank rel=noopener title="${esc(item.mime_type || '')}">Open original ↗</a></div>
    <div class=view><p id=pgerr class=muted hidden>This page image is not available; use "Open original".</p><img id=pgimg alt="${esc(item.filename)}"></div></aside>`;
}
function showPage(){
  const img = document.getElementById('pgimg'); if (!img) return;
  document.getElementById('pg').textContent = `page ${viewer.page} of ${viewer.pages}`;
  img.style.width = (viewer.zoom * 100) + '%';
  const err = document.getElementById('pgerr');
  img.hidden = false; err.hidden = true;
  img.onerror = () => { img.hidden = true; err.hidden = false; };
  img.src = `/api/documents/${encodeURIComponent(viewer.id)}/pages/${viewer.page}`;
}
function turn(n){ const p = viewer.page + n; if (p >= 1 && p <= viewer.pages){ viewer.page = p; showPage(); } }
function zoomBy(f){ viewer.zoom = f ? Math.min(4, Math.max(0.5, viewer.zoom * f)) : 1; showPage(); }
async function decide(fact, action, item){ const r = await post(`/api/facts/${fact}/review`, {action}); if (!r.ok) alert((await r.json()).detail); openItem(item); }
async function approve(id){
  // one approval per click: the buttons stay disabled while the bundle is built and validated
  // (seconds); afterwards the panel is the result, with no Edit / Approve left on an approved item
  document.querySelectorAll('#item button').forEach(b => b.disabled = true);
  $('#msg').textContent = 'Approving… building and validating the FHIR bundle';
  const r = await post(`/api/reviewer/items/${id}/approve`, {}); const j = await r.json();
  if (!r.ok){
    $('#msg').textContent = 'Error: ' + j.detail;
    document.querySelectorAll('#item button').forEach(b => b.disabled = false);
    return;
  }
  $('#item').innerHTML = `<h2 style="margin-top:0">Approved</h2>
    <p>Promoted into the canonical EMR: ${esc(Object.entries(j.promoted).map(([t, n]) => `${n} ${t.replace('cn_', '')}`).join(', ') || 'nothing')} ·
      bundle <span class="pill ${j.status === 'ready_to_share' ? 'ok' : 'warn'}">${esc(j.status)}</span> (${esc(j.validation.engine)})</p>
    <p><a href="${download(id)}">Download FHIR bundle</a> — it stays under <b>Approved</b> below.</p>`;
  load();
}
async function rejectItem(id){ const reason = prompt('Reason?') || ''; const r = await post(`/api/reviewer/items/${id}/reject`, {reason}); if (!r.ok) alert((await r.json()).detail); load(); }
$('#load').onclick = load;
load();
"""


def reviewer_page() -> str:
    return page("Reviewer — CareFlow", BODY, active="/reviewer", script=SCRIPT)
