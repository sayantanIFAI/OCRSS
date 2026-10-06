"""``GET /reviewer/c360?mpi=`` — Customer 360 (backlog E7-S8, ARCHITECTURE §4.5 / §7.1): one
patient's demographics, identifiers (ABHA and legacy MRN side by side), documents and canonical
clinical facts across documents. Read-only; opened from the Reviewer. Vanilla JS, no CDN."""

from __future__ import annotations

from cdi_adapter.webapp.theme import page

BODY = """
<p style="margin:0 0 6px"><a href="/reviewer">&larr; Reviewer</a></p>
<h1 style="margin:0 0 4px">Customer 360</h1>
<p class=muted>One patient's canonical EMR across every document: what reviewers approved and promoted.</p>
<form id=find style="margin:0 0 16px"><input id=mpi placeholder="MPI id, e.g. CFP-…" style="min-width:260px">
  <button class=primary>Open</button></form>
<div id=out class=muted>Open a patient from the Reviewer, or enter an MPI id.</div>
"""

SCRIPT = r"""
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const day = v => v ? String(v).slice(0, 10) : '';
const num = (n, u) => n == null ? '' : `${n}${u ? ' ' + u : ''}`;
const code = r => r.code || r.test_code ? `<span class=muted>${esc(r.code || r.test_code)}</span>` : '';
const src = r => `<small class=muted>${esc(r.document || '')}${r.recorded_at ? ' · ' + day(r.recorded_at) : ''}</small>`;
const SECTIONS = [
  ['conditions', 'Conditions', r => [esc(r.display), esc(r.clinical_status), code(r)]],
  ['medications', 'Medications', r => [esc(r.drug_text), esc(num(r.strength_num, r.strength_unit)),
    esc([r.frequency_code, r.duration_days != null ? r.duration_days + ' d' : '', r.route].filter(Boolean).join(' · ')), code(r)]],
  ['lab_results', 'Lab results', r => [esc(r.test_display), esc(r.value_num != null ? num(r.value_num, r.unit_ucum) : r.value_text),
    esc(r.abnormal_flag), code(r)]],
  ['lab_orders', 'Tests advised', r => [esc(r.test_text), esc(r.status), code(r)]],
  ['vital_signs', 'Vital signs', r => [esc(r.display), esc(r.value_num != null ? num(r.value_num, r.unit_ucum) : r.value_text),
    esc(day(r.effective_time)), code(r)]],
  ['allergies', 'Allergies', r => [esc(r.substance), esc(r.reaction), code(r)]],
];
function table(rows, cells){
  return `<div style="overflow-x:auto"><table>${rows.map(r => `<tr>${cells(r).map(c => `<td>${c}</td>`).join('')}<td>${src(r)}</td></tr>`).join('')}</table></div>`;
}
async function open360(mpi){
  if (!mpi) return;
  history.replaceState(null, '', '/reviewer/c360?mpi=' + encodeURIComponent(mpi));
  $('#mpi').value = mpi;
  const r = await fetch('/api/reviewer/c360/' + encodeURIComponent(mpi));
  if (!r.ok){ $('#out').innerHTML = `<p class=muted>${esc((await r.json()).detail || 'Not found')}</p>`; return; }
  const d = await r.json(), p = d.patient;
  const ids = d.identifiers.map(i => `<tr><td>${esc(i.type)}</td><td><b>${esc(i.value)}</b></td></tr>`).join('');
  const docs = d.documents.length ? `<table><tr><th>Document</th><th>Type</th><th>Pipeline</th><th>Review</th><th>Received</th></tr>${d.documents.map(x =>
    `<tr><td>${esc(x.filename)}</td><td>${esc(x.doc_type)}</td><td>${esc(x.status)}</td><td>${esc(x.review_status || '')}</td><td>${day(x.created_at)}</td></tr>`).join('')}</table>` : '<p class=muted>None.</p>';
  const secs = SECTIONS.map(([k, label, cells]) => `<h3 style="margin:18px 0 6px">${label} <span class=muted style="font-weight:400;font-size:14px">${d[k].length}</span></h3>` +
    (d[k].length ? table(d[k], cells) : '<p class=muted>None promoted.</p>')).join('');
  const bundles = d.bundles.length ? `<table>${d.bundles.map(b => `<tr><td>${esc(b.artifact_type)}</td><td>${b.status === 'ready_to_share' ? '<span class="pill ok">ready to share</span>' : `<span class="pill warn">${esc(b.status)}</span>`}</td><td>${esc(b.document || '')}</td><td>${day(b.created_at)}</td></tr>`).join('')}</table>` : '<p class=muted>None.</p>';
  $('#out').classList.remove('muted');
  $('#out').innerHTML = `<div class=grid style="grid-template-columns:1fr 1fr">
    <section class=card><h2 style="margin-top:0">${esc(p.name || 'Unnamed patient')}</h2>
      <p>${esc(p.sex || '')}${p.birth_date ? ' · born ' + day(p.birth_date) : ''}${p.identity_confidence != null ? ` · identity confidence ${Number(p.identity_confidence).toFixed(2)}` : ''}</p></section>
    <section class=card><h2 style="margin-top:0">Identifiers</h2><table>${ids}</table></section></div>
    <section class=card style="margin-top:16px"><h2 style="margin-top:0">Clinical record</h2>${secs}</section>
    <section class=card style="margin-top:16px"><h2 style="margin-top:0">Documents</h2>${docs}</section>
    <section class=card style="margin-top:16px"><h2 style="margin-top:0">FHIR bundles</h2>${bundles}</section>`;
}
$('#find').onsubmit = e => { e.preventDefault(); open360($('#mpi').value.trim()); };
open360(new URLSearchParams(location.search).get('mpi'));
"""


def c360_page() -> str:
    return page("Customer 360 — CareFlow", BODY, active="/reviewer", script=SCRIPT)
