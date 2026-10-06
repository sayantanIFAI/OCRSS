"""``GET /review`` — the S8 console (ARCHITECTURE §5 S8): page image -> highlighted bbox -> OCR span
-> fact -> code -> confidence -> findings -> accept / correct / reject. Vanilla JS, no CDN."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from cdi_adapter.webapp.theme import page

router = APIRouter()

BODY = """
<h1 style="margin:0 0 4px">Review queue</h1>
<p class=muted>Held facts are never shared until a clinician accepts, corrects or rejects them.</p>
<div class=grid style="grid-template-columns:minmax(260px,1fr) 2fr">
  <section class=card><h2 style="margin-top:0">Open tasks</h2><div id=tasks>Loading…</div></section>
  <section class=card id=detail><p class=muted>Select a fact.</p></section>
</div>
"""

SCRIPT = r"""
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function loadTasks(){
  const tasks = await (await fetch('/api/review/tasks')).json();
  $('#tasks').innerHTML = tasks.length ? tasks.map(t => `
    <div style="margin-bottom:12px"><b>${esc(t.filename||t.document_id)}</b>
      <span class="pill ${t.kind==='dose_check'?'bad':'warn'}">${esc(t.kind)}</span>
      <ul style="padding-left:18px;margin:6px 0">${t.facts.map(f => `
        <li><a href="#" data-fact="${f.id}">${esc(f.fact_type)}: ${esc(f.local_text)}</a>
          <span class=muted>${(f.confidence_overall??0).toFixed(2)}</span></li>`).join('')}</ul></div>`).join('')
    : '<p class=muted>No open tasks.</p>';
  document.querySelectorAll('[data-fact]').forEach(a => a.onclick = e => {e.preventDefault(); showFact(a.dataset.fact);});
}
async function showFact(id){
  const d = await (await fetch('/api/review/facts/' + id)).json();
  const f = d.fact, p = (d.provenance||[])[0] || {};
  const box = p.bbox_union;
  $('#detail').innerHTML = `
    <h2 style="margin-top:0">${esc(f.fact_type)} <span class=pill>${esc(f.review_state)}</span></h2>
    <div style="position:relative;display:inline-block;max-width:100%">
      ${p.page_image_url ? `<img id=pg src="${p.page_image_url}" style="max-width:100%;border:1px solid var(--border)">` : '<p class=muted>No page image.</p>'}
      <div id=hl style="position:absolute;border:3px solid var(--bad);display:none"></div></div>
    <table><tr><th>OCR span</th><td>${esc(p.extracted_text)}</td></tr>
      <tr><th>Fact</th><td>${esc(f.local_text)} ${f.value_num??''} ${esc(f.value_unit_ucum)} ${esc(f.value_text)}</td></tr>
      <tr><th>Code</th><td>${esc(f.code_system)} ${esc(f.code)} ${esc(f.code_display)}</td></tr>
      <tr><th>Confidence</th><td>${(f.confidence_overall??0).toFixed(3)}</td></tr>
      <tr><th>Findings</th><td>${(d.findings||[]).map(x => `<span class="pill ${x.severity==='blocker'?'bad':'warn'}">${esc(x.code)}</span> ${esc(x.message)}`).join('<br>')||'—'}</td></tr></table>
    <p><label>Reviewer <input id=rv value="${esc(localStorage.getItem('reviewer')||'')}" placeholder="your id"></label></p>
    <p><label>Correct text <input id=c_text value="${esc(f.local_text)}"></label>
       <label>value <input id=c_val size=6 value="${f.value_num??''}"></label>
       <label>code <input id=c_code size=12 value="${esc(f.code)}"></label></p>
    <p><button class=ok onclick="act('${f.id}','accept')">Accept</button>
       <button class=primary onclick="act('${f.id}','correct')">Correct</button>
       <button class=bad onclick="act('${f.id}','reject')">Reject</button> <span id=msg></span></p>`;
  const img = $('#pg');
  if (img && box) img.onload = () => {
    const s = img.clientWidth / img.naturalWidth, h = $('#hl');
    Object.assign(h.style, {left:box[0]*s+'px', top:box[1]*s+'px', width:(box[2]-box[0])*s+'px',
                            height:(box[3]-box[1])*s+'px', display:'block'});
  };
}
async function act(id, action){
  const reviewer = $('#rv').value.trim() || 'reviewer';
  localStorage.setItem('reviewer', reviewer);
  const body = {action, reviewer};
  if (action === 'correct'){
    body.corrections = {local_text: $('#c_text').value};
    if ($('#c_val').value !== '') body.corrections.value_num = Number($('#c_val').value);
    if ($('#c_code').value !== '') body.corrections.code = $('#c_code').value;
  }
  const r = await fetch('/api/facts/' + id + '/review', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  $('#msg').textContent = r.ok ? 'Saved.' : ('Error: ' + (await r.json()).detail);
  if (r.ok) loadTasks();
}
loadTasks();
"""


@router.get("/review", response_class=HTMLResponse)
def review_page() -> str:
    return page("Review queue — CareFlow", BODY, active="/review", script=SCRIPT)
