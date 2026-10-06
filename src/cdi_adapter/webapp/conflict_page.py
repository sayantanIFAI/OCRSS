"""``GET /conflicts`` - the merge console (backlog E5-S3; ARCHITECTURE §5 S8).

The S8 console decides one fact at a time. This page is for the case the S6 gate holds *two* facts
over: both are shown **side by side with their own evidence** - each one's page image with its bbox
drawn, the OCR span behind it, the value, the code, the confidence and the findings - and the
reviewer picks **keep A**, **keep B** or **merge**. Merge opens the kept fact's fields so the
reviewer types what the merged fact should say; it is sent as one request and lands as one
auditable decision (``webapp/conflicts.py``). Vanilla JS, no CDN.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from cdi_adapter.webapp.theme import page

router = APIRouter()

BODY = """
<h1 style="margin:0 0 4px">Merge conflicts</h1>
<p class=muted>Two facts of one patient disagree. Nothing is shared until a clinician settles it &mdash; pick the one that matches the evidence, or merge them.</p>
<div class=grid style="grid-template-columns:minmax(240px,1fr) 3fr">
  <section class=card><h2 style="margin-top:0">Open conflicts</h2><div id=list>Loading&hellip;</div></section>
  <section class=card id=detail><p class=muted>Select a conflict.</p></section>
</div>
"""

SCRIPT = r"""
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num = v => (v === null || v === undefined || v === '') ? '' : v;
let current = null;

function value(f){
  return [esc(f.local_text), num(f.value_num), esc(f.value_unit_ucum), esc(f.value_text)]
    .filter(x => x !== '' && x !== undefined).join(' ');
}

async function loadList(){
  const rows = await (await fetch('/api/conflicts')).json();
  $('#list').innerHTML = rows.length ? rows.map(c => `
    <div style="margin-bottom:12px">
      <a href="#" data-conflict="${c.id}"><b>${esc(c.a.fact_type || 'fact')}</b></a>
      <span class="pill ${c.severity==='blocker'?'bad':'warn'}">${esc(c.conflict_type)}</span>
      <div class=muted>${esc(c.patient_name || c.patient_id)}</div>
      <div class=muted style="font-size:12px">A: ${value(c.a)} &nbsp;&middot;&nbsp; B: ${value(c.b)}</div>
    </div>`).join('')
    : '<p class=muted>No open conflicts.</p>';
  document.querySelectorAll('[data-conflict]').forEach(a =>
    a.onclick = e => {e.preventDefault(); show(a.dataset.conflict);});
}

function evidence(side, label){
  const f = side.fact, p = (side.provenance || [])[0] || {};
  const findings = (side.findings || []).map(x =>
    `<span class="pill ${x.severity==='blocker'?'bad':'warn'}">${esc(x.code)}</span> ${esc(x.message)}`
  ).join('<br>') || '&mdash;';
  return `
    <div class=card style="margin:0">
      <h3 style="margin:0 0 6px">${label} <span class=pill>${esc(f.review_state)}</span></h3>
      <div style="position:relative;display:inline-block;max-width:100%">
        ${p.page_image_url
          ? `<img src="${p.page_image_url}" data-box="${esc(JSON.stringify(p.bbox_union||null))}"
                  onload="drawBox(this)" style="max-width:100%;border:1px solid var(--border)">`
          : '<p class=muted>No page image.</p>'}
      </div>
      <table>
        <tr><th>Value</th><td><b>${value(f)}</b></td></tr>
        <tr><th>OCR span</th><td>${esc(p.extracted_text)}</td></tr>
        <tr><th>Code</th><td>${esc(f.code_system)} ${esc(f.code)} ${esc(f.code_display)}</td></tr>
        <tr><th>Confidence</th><td>${(f.confidence_overall ?? 0).toFixed(3)}</td></tr>
        <tr><th>Recorded</th><td>${esc(p.agent)} ${esc(p.recorded_at)}</td></tr>
        <tr><th>Findings</th><td>${findings}</td></tr>
      </table>
    </div>`;
}

function drawBox(img){
  let box = null;
  try { box = JSON.parse(img.dataset.box); } catch (e) { box = null; }
  if (!box || box.length !== 4) return;
  const d = document.createElement('div');
  const sx = img.clientWidth / img.naturalWidth, sy = img.clientHeight / img.naturalHeight;
  Object.assign(d.style, {position:'absolute', border:'3px solid var(--bad)',
    left: (box[0]*sx)+'px', top: (box[1]*sy)+'px',
    width: ((box[2]-box[0])*sx)+'px', height: ((box[3]-box[1])*sy)+'px'});
  img.parentElement.appendChild(d);
}

async function show(id){
  const d = await (await fetch('/api/conflicts/' + id)).json();
  current = d;
  const c = d.conflict;
  $('#detail').innerHTML = `
    <h2 style="margin-top:0">${esc(c.conflict_type)}
      <span class="pill ${c.severity==='blocker'?'bad':'warn'}">${esc(c.severity)}</span>
      <span class=pill>${esc(c.evidence_state)}</span></h2>
    <div class=grid style="grid-template-columns:1fr 1fr">
      ${evidence(d.a, 'A')}${evidence(d.b, 'B')}
    </div>
    <p style="margin:14px 0 6px;display:flex;gap:8px;flex-wrap:wrap">
      <button id=keepa class=primary>Keep A</button>
      <button id=keepb class=primary>Keep B</button>
      <button id=mergebtn>Merge&hellip;</button>
    </p>
    <div id=mergebox style="display:none">
      <p class=muted>The merged fact is a correction of the fact you keep: it supersedes it, and the other is rejected.</p>
      <p><label>Keep as the base
        <select id=base><option value=keep_a>A</option><option value=keep_b>B</option></select></label></p>
      <p><label>Text<br><input id=m_local_text style="width:90%"></label></p>
      <p><label>Value<br><input id=m_value_num type=number step=any> <input id=m_value_unit_ucum placeholder=unit></label></p>
      <button id=domerge class=primary>Save merge</button>
    </div>
    <p><label>Note (why)<br><input id=note style="width:90%" placeholder="what the evidence shows"></label></p>
    <p id=msg class=muted></p>`;
  $('#keepa').onclick = () => send({choice:'keep_a'});
  $('#keepb').onclick = () => send({choice:'keep_b'});
  $('#mergebtn').onclick = () => {
    const box = $('#mergebox');
    box.style.display = box.style.display === 'none' ? 'block' : 'none';
    const f = d.a.fact;
    $('#m_local_text').value = f.local_text ?? '';
    $('#m_value_num').value = f.value_num ?? '';
    $('#m_value_unit_ucum').value = f.value_unit_ucum ?? '';
  };
  $('#domerge').onclick = () => {
    const corrections = {};
    const text = $('#m_local_text').value.trim();
    const v = $('#m_value_num').value, unit = $('#m_value_unit_ucum').value.trim();
    if (text) corrections.local_text = text;
    if (v !== '') corrections.value_num = Number(v);
    if (unit) corrections.value_unit_ucum = unit;
    if (!Object.keys(corrections).length) { $('#msg').textContent = 'Merge needs at least one value.'; return; }
    send({choice:'merge', base: $('#base').value === 'keep_b' ? 'b' : 'a', corrections});
  };
}

async function send(body){
  body.note = $('#note').value.trim() || null;
  const r = await fetch('/api/conflicts/' + current.conflict.id + '/resolve',
    {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const out = await r.json();
  $('#msg').textContent = r.ok ? `Settled: ${out.resolution}.` : ('Error: ' + (out.detail || r.status));
  if (r.ok) { $('#detail').innerHTML = '<p class=muted>Select a conflict.</p>'; loadList(); }
}

window.drawBox = drawBox;
loadList();
"""


@router.get("/conflicts", response_class=HTMLResponse)
def conflicts_page() -> str:
    return page("Merge conflicts — CareFlow", BODY, active="/conflicts", script=SCRIPT)
