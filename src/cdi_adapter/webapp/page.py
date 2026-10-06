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
  <p style="margin:10px 0 0;display:flex;gap:8px;flex-wrap:wrap">
    <button id=cam type=button title="Take a photo of a document with this device's camera">&#128247; Take photo</button>
    <button id=browse type=button title="Choose scans or photos saved on this device">&#128193; Upload files</button>
    <input id=camfile type=file accept="image/*" capture=environment hidden>
  </p>
  <p id=picked class=muted></p>
  <button id=go class=primary disabled>Generate EMR</button>
</section>
<div id=campanel style="display:none;position:fixed;inset:0;background:rgba(15,27,51,.72);z-index:50;align-items:center;justify-content:center;padding:16px">
  <div class=card style="max-width:720px;width:100%;margin:0">
    <h2 style="margin-top:0">Take a photo of the document</h2>
    <p class=muted style="margin-top:0">Hold the page flat, fill the frame, avoid glare. The photo is added to the scans above.</p>
    <video id=camvideo autoplay playsinline muted style="width:100%;max-height:60vh;background:#000;border-radius:var(--radius)"></video>
    <img id=camshot alt="Captured photo" hidden style="width:100%;max-height:60vh;object-fit:contain;background:#000;border-radius:var(--radius)">
    <p id=cammsg class=muted></p>
    <p style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:0">
      <button id=camsnap type=button class=primary>Capture</button>
      <button id=camuse type=button class=primary hidden>Use photo</button>
      <button id=camretake type=button hidden>Retake</button>
      <button id=camclose type=button>Cancel</button>
    </p>
  </div>
</div>
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
// Camera (2026-09-29): a photo taken here is ADDED to the chosen scans as a normal JPEG file and goes
// through the same upload and pipeline as a chosen file; nothing about processing changes. The live
// camera needs a secure page (https or localhost); without it the phone's own camera app is used.
$('#browse').onclick = () => $('#files').click();
const cam = {stream: null, blob: null};
const stamp = () => new Date().toISOString().replace(/[-:]/g, '').replace('T', '-').slice(0, 15);
function addPhoto(blob){  // a File (from the phone's camera app) is kept as it is
  if (chosen.length >= 10){ alert('Up to 10 scans per upload: remove some or upload these first.'); return false; }
  const file = blob instanceof File ? blob
    : new File([blob], `camera-${stamp()}.jpg`, {type: 'image/jpeg', lastModified: Date.now()});
  pick(chosen.concat([file]));
  return true;
}
function camShow(on){ $('#campanel').style.display = on ? 'flex' : 'none'; }
function camStop(){ if (cam.stream){ cam.stream.getTracks().forEach(t => t.stop()); cam.stream = null; } }
function camReset(){
  cam.blob = null; $('#camshot').hidden = true; $('#camvideo').hidden = false;
  $('#camsnap').hidden = false; $('#camuse').hidden = true; $('#camretake').hidden = true; $('#cammsg').textContent = '';
}
$('#cam').onclick = async () => {
  if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) || !window.isSecureContext){
    $('#camfile').click(); return;  // the device's own camera (phones), or a file chooser
  }
  camReset(); camShow(true); $('#cammsg').textContent = 'Starting the camera…';
  try {
    cam.stream = await navigator.mediaDevices.getUserMedia({audio: false,
      video: {facingMode: {ideal: 'environment'}, width: {ideal: 1920}, height: {ideal: 1080}}});
    $('#camvideo').srcObject = cam.stream; $('#cammsg').textContent = '';
  } catch (e) {
    camShow(false); camStop();
    $('#picked').textContent = 'Camera not available (' + (e.name || e) + '): choose a photo instead.';
    $('#camfile').click();
  }
};
$('#camsnap').onclick = () => {
  const v = $('#camvideo');
  if (!v.videoWidth){ $('#cammsg').textContent = 'The camera is not ready yet.'; return; }
  const c = document.createElement('canvas'); c.width = v.videoWidth; c.height = v.videoHeight;
  c.getContext('2d').drawImage(v, 0, 0);
  c.toBlob(b => {
    cam.blob = b; $('#camshot').src = URL.createObjectURL(b);
    $('#camshot').hidden = false; v.hidden = true;
    $('#camsnap').hidden = true; $('#camuse').hidden = false; $('#camretake').hidden = false;
  }, 'image/jpeg', 0.92);
};
$('#camretake').onclick = camReset;
$('#camuse').onclick = () => { if (cam.blob && addPhoto(cam.blob)){ camStop(); camShow(false); } };
$('#camclose').onclick = () => { camStop(); camShow(false); };
$('#camfile').onchange = e => { const f = e.target.files[0]; if (f) addPhoto(f); e.target.value = ''; };
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
  const age = p.age_text ? 'age ' + p.age_text : '';
  $('#banner').innerHTML = `<div class=card style="background:var(--brand-weak)"><b>${esc(p.name || 'Unnamed patient')}</b> · ${[p.mpi_id, p.sex, age, p.birth_date].filter(Boolean).map(esc).join(' · ')} · identity confidence ${p.identity_confidence ?? '—'}</div>`;
  const held = j.documents.reduce((n, d) => n + (d.in_review || 0), 0);
  const facts = j.documents.reduce((n, d) => n + (d.facts || 0), 0);
  // a document not recognised as a clinical type is skipped: say so, never "All facts governed"
  const skipped = j.documents.filter(d => d.status === 'skipped' || d.status === 'error');
  const notDone = skipped.map(d => `<p><span class="pill bad">not processed</span> <b>${esc(d.filename)}</b> — ${d.status === 'error' ? 'failed: ' + esc(d.error) : `read as "${esc(d.doc_type)}", not a clinical document type, so nothing was extracted and nothing goes to review`}.</p>`).join('');
  const verdict = held ? `<span class="pill warn">${held} fact(s) sent to the reviewer</span> — see <a href="/reviewer">Reviewer</a> or <a href="/review">Review</a>.`
    : facts ? '<span class="pill ok">All facts governed</span>' : '<span class="pill warn">No facts extracted</span>';
  $('#after').innerHTML = `${notDone}<p>${verdict}</p>
    <p><a href="/api/jobs/${id}/fhir/download">Download FHIR record</a> <small>(this upload only; it holds confirmed facts only${held ? ' — confirm the held facts first to include them' : ''})</small></p>
    <details${p.name ? '' : ' open'}><summary>Complete patient details</summary>
      <p><input id=r_name placeholder="name (required)" aria-label=name>
      <select id=r_gender aria-label=gender><option value="">gender</option><option value=M>Male</option><option value=F>Female</option><option value=O>Other</option><option value=U>Unknown</option></select>
      <input id=r_mobile placeholder="mobile (required)"> <input id=r_dob placeholder="date of birth YYYY-MM-DD (required)">
      <input id=r_addr placeholder=address> <input id=r_abha placeholder=ABHA>
      <button onclick="saveReg('${esc(p.mpi_id)}')">Save</button> <span id=r_msg></span></p>
      ${age && !p.birth_date ? `<p class=muted>The document gives ${esc(age)} but no date of birth; the registry needs the date of birth.</p>` : ''}</details>`;
  // prefilled with what the documents gave, editable: a document without a legible name must not
  // leave the form unsaveable ("new registry patient needs name")
  $('#r_name').value = p.name || '';
  $('#r_gender').value = ['M', 'F', 'O', 'U'].includes(p.sex) ? p.sex : '';
  $('#r_dob').value = p.birth_date || '';
}
async function saveReg(pid){
  const body = {patient_id: pid, name: $('#r_name').value.trim() || null, gender: $('#r_gender').value || null,
                mobile: $('#r_mobile').value || null, dob: $('#r_dob').value || null,
                address: $('#r_addr').value || null, abha_id: $('#r_abha').value || null};
  const r = await fetch('/api/registry', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  $('#r_msg').textContent = r.ok ? 'Saved.' : 'Error: ' + JSON.stringify((await r.json()).detail);
}
"""


def upload_page() -> str:
    return page("Generate EMR — CareFlow", BODY, active="/", script=SCRIPT)
