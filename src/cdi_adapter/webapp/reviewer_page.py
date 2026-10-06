"""``GET /reviewer`` — workbasket -> worklist console (ARCHITECTURE §4.5). Vanilla JS, no CDN."""

from __future__ import annotations

from cdi_adapter.webapp.theme import page

BODY = """
<h1 style="margin:0 0 4px">Reviewer</h1>
<p class=muted>Claim an item into your worklist, decide its held facts, then approve to promote it into the canonical EMR and generate its FHIR bundle.</p>
<p><label>Reviewer id <input id=rv placeholder="e.g. dr.sen"></label> <button id=load class=primary>Load</button></p>
<div class=grid style="grid-template-columns:1fr 1fr">
  <section class=card><h2 style="margin-top:0">Workbasket</h2><div id=wb class=muted>—</div></section>
  <section class=card><h2 style="margin-top:0">My worklist</h2><div id=wl class=muted>—</div></section>
</div>
<section class=card id=item style="margin-top:16px"><p class=muted>Open an item from your worklist.</p></section>
"""

SCRIPT = r"""
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
$('#rv').value = localStorage.getItem('reviewer') || '';
const who = () => { const r = $('#rv').value.trim(); localStorage.setItem('reviewer', r); return r; };
const post = (url, body) => fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
const row = (i, btn) => `<tr><td>${esc(i.patient_name)} <span class=muted>${esc(i.mpi_id)}</span><br><small>${esc(i.filename)} · ${esc(i.doc_type)}</small></td>
  <td>${i.held ? `<span class="pill warn">${i.held} held</span>` : '<span class="pill ok">ready</span>'} ${i.elements} facts</td><td>${btn}</td></tr>`;
async function load(){
  const r = who(); if (!r) { alert('Enter your reviewer id'); return; }
  const wb = await (await fetch('/api/reviewer/workbasket')).json();
  $('#wb').innerHTML = wb.length ? '<table>' + wb.map(i => row(i, `<button onclick="claim('${i.id}')">Claim</button>`)).join('') + '</table>' : 'Empty.';
  const wl = await (await fetch('/api/reviewer/worklist?reviewer=' + encodeURIComponent(r))).json();
  $('#wl').innerHTML = wl.length ? '<table>' + wl.map(i => row(i, `<button onclick="openItem('${i.id}')">Open</button>`)).join('') + '</table>' : 'Empty.';
}
async function claim(id){ const r = await post(`/api/reviewer/items/${id}/claim`, {reviewer: who()}); if (!r.ok) alert((await r.json()).detail); load(); }
async function openItem(id){
  const d = await (await fetch(`/api/reviewer/items/${id}/elements`)).json();
  $('#item').innerHTML = `<h2 style="margin-top:0">${esc(d.item.patient_name)} — ${esc(d.item.filename)}</h2>
    <table><tr><th>Type</th><th>Fact</th><th>Code</th><th>State</th><th></th></tr>${d.elements.map(e => `<tr>
      <td>${esc(e.element_type)}</td><td>${esc(e.fact?.local_text)} ${e.fact?.value_num ?? ''} ${esc(e.fact?.value_unit_ucum)}</td>
      <td>${esc(e.fact?.code)}</td><td><span class="pill ${e.state==='held'?'warn':e.state==='rejected'?'bad':'ok'}">${esc(e.state)}</span></td>
      <td>${e.state==='held' ? `<button class=ok onclick="decide('${e.fact.id}','accept','${id}')">Accept</button> <button class=bad onclick="decide('${e.fact.id}','reject','${id}')">Reject</button> <a href="/review">details</a>` : ''}</td></tr>`).join('')}</table>
    <p><button class=primary onclick="approve('${id}')">Approve &amp; promote</button> <button onclick="rejectItem('${id}')">Reject item</button> <span id=msg></span></p><pre id=out style="white-space:pre-wrap"></pre>`;
}
async function decide(fact, action, item){ const r = await post(`/api/facts/${fact}/review`, {action, reviewer: who()}); if (!r.ok) alert((await r.json()).detail); openItem(item); }
async function approve(id){
  const r = await post(`/api/reviewer/items/${id}/approve`, {reviewer: who()}); const j = await r.json();
  $('#msg').textContent = r.ok ? `Promoted: ${JSON.stringify(j.promoted)} · bundle ${j.status} (${j.validation.engine})` : 'Error: ' + j.detail;
  if (r.ok) load();
}
async function rejectItem(id){ const reason = prompt('Reason?') || ''; const r = await post(`/api/reviewer/items/${id}/reject`, {reviewer: who(), reason}); if (!r.ok) alert((await r.json()).detail); load(); }
$('#load').onclick = load;
"""


def reviewer_page() -> str:
    return page("Reviewer — CareFlow", BODY, active="/reviewer", script=SCRIPT)
