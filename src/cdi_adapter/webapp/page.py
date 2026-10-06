"""``GET /`` — the Generate EMR upload page (ARCHITECTURE §7, §7.2). No name/sex fields: identity is
read from the documents (or an existing registry patient is looked up). Vanilla JS, no CDN."""

from __future__ import annotations

from cdi_adapter.webapp.theme import page

BODY = """
<h1 style="margin:0">Intelligent OCR driven Agentic EMR generation</h1>
<p class=muted>Upload a patient's scanned records. Identity is read from the documents; nothing is shared until every fact is governed.</p>
<div class=grid style="grid-template-columns:1fr 1fr">
<section class=card>
  <h2 style="margin-top:0">Generate EMR</h2>
  <p><label>Existing patient (CareFlow id / ABHA / mobile)<br>
     <input id=q placeholder="optional" style="width:70%"> <button id=lookup type=button>Look up</button></label></p>
  <p id=found class=muted></p>
  <p><label>ABHA number (optional)<br><input id=abha placeholder="91-XXXX-XXXX-XXXX"></label></p>
  <div id=drop class=card style="border-style:dashed;text-align:center;padding:28px;cursor:pointer">
    Drop up to 10 scans here, or click to choose<input id=files type=file multiple accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff" hidden>
  </div>
  <p id=picked class=muted></p>
  <button id=go class=primary disabled>Generate EMR</button>
</section>
<section class=card>
  <h2 style="margin-top:0">Progress</h2>
  <div id=banner></div>
  <table id=grid><thead><tr><th>Document</th><th>Ingest</th><th>OCR</th><th>Classify</th><th>Extract</th><th>Terminology</th><th>Validate</th></tr></thead><tbody></tbody></table>
  <div id=after></div>
</section>
</div>
"""

SCRIPT = r"""
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const STEPS = ['ingest','ocr','classify','extract','terminology','validate'];
let chosen = [], patientRef = null;
$('#drop').onclick = () => $('#files').click();
$('#drop').ondragover = e => { e.preventDefault(); };
$('#drop').ondrop = e => { e.preventDefault(); pick([...e.dataTransfer.files]); };
$('#files').onchange = e => pick([...e.target.files]);
function pick(fs){ chosen = fs.slice(0, 10); $('#picked').textContent = chosen.map(f => f.name).join(', '); $('#go').disabled = !chosen.length; }
$('#lookup').onclick = async () => {
  const r = await fetch('/api/registry/lookup?q=' + encodeURIComponent($('#q').value));
  if (r.ok){ const p = await r.json(); patientRef = p.patient_id; $('#found').innerHTML = `Found <b>${esc(p.name)}</b> (${esc(p.patient_id)}) — documents will attach to this patient.`; }
  else { patientRef = null; $('#found').textContent = 'No registered patient found; identity will be read from the documents.'; }
};
$('#go').onclick = async () => {
  const fd = new FormData(); chosen.forEach(f => fd.append('files', f));
  if ($('#abha').value) fd.append('abha', $('#abha').value);
  if (patientRef) fd.append('patient_ref', patientRef);
  $('#go').disabled = true;
  const r = await fetch('/api/jobs', {method:'POST', body:fd});
  if (!r.ok){ $('#after').textContent = 'Upload failed: ' + (await r.json()).detail; $('#go').disabled = false; return; }
  poll((await r.json()).job_id);
};
async function poll(id){
  const j = await (await fetch('/api/jobs/' + id)).json();
  $('#grid tbody').innerHTML = j.documents.map(d => `<tr><td>${esc(d.filename)} <span class=muted>${esc(d.doc_type)}</span></td>${
    STEPS.map(s => `<td>${d.steps_done.includes(s) ? '✔' : (d.step === s ? '…' : (d.status === 'error' ? '✖' : ''))}</td>`).join('')}</tr>`).join('');
  if (j.state === 'mismatch'){
    $('#banner').innerHTML = `<div class=card style="border-color:var(--bad);background:var(--bad-weak)"><b>Stopped: "${esc(j.mismatch.filename)}" does not belong to the selected patient.</b> No facts were kept from it.</div>`;
    return;
  }
  if (j.state === 'running'){ setTimeout(() => poll(id), 1500); return; }
  const p = j.patient || {};
  $('#banner').innerHTML = `<div class=card style="background:var(--brand-weak)"><b>${esc(p.name || 'Unnamed patient')}</b> · ${esc(p.mpi_id)} · ${esc(p.sex)} · ${esc(p.birth_date)} · identity confidence ${p.identity_confidence ?? '—'}</div>`;
  const held = j.documents.reduce((n, d) => n + d.in_review, 0);
  $('#after').innerHTML = `<p>${held ? `<span class="pill warn">${held} fact(s) sent to the reviewer</span> — see <a href="/reviewer">Reviewer</a> or <a href="/review">Review</a>.` : '<span class="pill ok">All facts governed</span>'}</p>
    <p><a href="/api/jobs/${id}/fhir/download">Download FHIR bundles</a></p>
    <details><summary>Complete patient details</summary>
      <p><input id=r_mobile placeholder=mobile> <input id=r_dob placeholder="YYYY-MM-DD"> <input id=r_addr placeholder=address> <input id=r_abha placeholder=ABHA>
      <button onclick="saveReg('${esc(p.mpi_id)}','${esc(p.name)}')">Save</button> <span id=r_msg></span></p></details>`;
}
async function saveReg(pid, name){
  const body = {patient_id: pid, name, mobile: $('#r_mobile').value || null, dob: $('#r_dob').value || null,
                address: $('#r_addr').value || null, abha_id: $('#r_abha').value || null};
  const r = await fetch('/api/registry', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  $('#r_msg').textContent = r.ok ? 'Saved.' : 'Error: ' + JSON.stringify((await r.json()).detail);
}
"""


def upload_page() -> str:
    return page("Generate EMR — CareFlow", BODY, active="/", script=SCRIPT)
