let streams = [];
let logTimer = null;

const $ = (id) => document.getElementById(id);

function notify(msg, error=false) {
  const n = $('notice');
  n.textContent = msg;
  n.className = `notice ${error ? 'error' : 'ok'}`;
  setTimeout(() => n.classList.add('hidden'), 4000);
}

async function api(url, options={}) {
  const res = await fetch(url, {
    headers: {'Content-Type':'application/json', ...(options.headers || {})},
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function loadStreams() {
  try {
    streams = await api('/api/streams');
    renderCards();
  } catch (e) { notify(e.message, true); }
}

function esc(s='') {
  return String(s).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}

function renderCards() {
  const cards = $('cards');
  cards.innerHTML = '';
  $('empty').classList.toggle('hidden', streams.length !== 0);
  for (const s of streams) {
    const enabled = (s.destinations || []).filter(d => d.enabled).length;
    const card = document.createElement('article');
    card.className = 'stream-card';
    card.innerHTML = `
      <div class="card-top">
        <div>
          <div class="status ${s.running ? 'live' : 'off'}"><span></span>${s.running ? 'LIVE' : 'STOPPED'}</div>
          <h3>${esc(s.name)}</h3>
          <div class="source">${esc(s.source)}</div>
        </div>
        <button class="icon-btn menu" onclick="editStream('${s.id}')">✎</button>
      </div>
      <div class="metrics">
        <div><strong>${esc(s.quality)}</strong><span>الجودة</span></div>
        <div><strong>${Number(s.bitrate).toLocaleString()}k</strong><span>Bitrate</span></div>
        <div><strong>${s.fps}</strong><span>FPS</span></div>
        <div><strong>${enabled}</strong><span>Outputs</span></div>
      </div>
      <div class="chips">
        ${(s.destinations || []).map(d => `<span class="chip ${d.enabled?'':'muted'}">${esc(d.name)}</span>`).join('') || '<span class="chip muted">لا توجد مخارج</span>'}
      </div>
      <div class="card-actions">
        ${s.running
          ? `<button class="btn danger grow" onclick="stopStream('${s.id}')">■ إيقاف</button>`
          : `<button class="btn primary grow" onclick="startStream('${s.id}')">▶ تشغيل</button>`}
        <button class="btn ghost" onclick="openLogs('${s.id}')">Logs</button>
        <button class="btn ghost danger-text" onclick="deleteStream('${s.id}')">حذف</button>
      </div>`;
    cards.appendChild(card);
  }
}

function resetForm() {
  $('streamForm').reset();
  $('streamId').value = '';
  $('quality').value = '1080p';
  $('bitrate').value = 5000;
  $('fps').value = 50;
  $('preset').value = 'superfast';
  $('logoWidth').value = 335;
  $('logoPosition').value = 'top-right';
  $('textSize').value = 38;
  $('textPosition').value = 'bottom-center';
  $('destinations').innerHTML = '';
  addDestination();
}

function openEditor(stream=null) {
  resetForm();
  if (stream) {
    $('modalTitle').textContent = 'تعديل البث';
    $('streamId').value = stream.id;
    $('name').value = stream.name || '';
    $('source').value = stream.source || '';
    $('quality').value = stream.quality || '1080p';
    $('bitrate').value = stream.bitrate || 5000;
    $('fps').value = stream.fps || 50;
    $('preset').value = stream.preset || 'superfast';
    $('logo').value = stream.logo || '';
    $('logoWidth').value = stream.logo_width || 335;
    $('logoPosition').value = stream.logo_position || 'top-right';
    $('text').value = stream.text || '';
    $('textSize').value = stream.text_size || 38;
    $('textPosition').value = stream.text_position || 'bottom-center';
    $('destinations').innerHTML = '';
    (stream.destinations || []).forEach(addDestination);
    if (!(stream.destinations || []).length) addDestination();
  } else {
    $('modalTitle').textContent = 'بث جديد';
  }
  $('modal').classList.remove('hidden');
}

function closeEditor() { $('modal').classList.add('hidden'); }

function editStream(id) {
  const s = streams.find(x => x.id === id);
  if (s?.running) return notify('أوقف البث قبل تعديل الإعدادات.', true);
  if (s) openEditor(s);
}

function addDestination(d={}) {
  const wrap = document.createElement('div');
  wrap.className = 'destination';
  wrap.dataset.id = d.id || crypto.randomUUID().slice(0,8);
  wrap.innerHTML = `
    <label class="check"><input type="checkbox" class="d-enabled" ${d.enabled === false ? '' : 'checked'}><span>فعال</span></label>
    <label>الاسم<input class="d-name" value="${esc(d.name || 'Telegram')}" placeholder="Telegram 1"></label>
    <label class="wide">RTMP / RTMPS Server<input class="d-base" value="${esc(d.rtmp_base || 'rtmps://dc4-1.rtmp.t.me/s/')}" placeholder="rtmps://.../s/"></label>
    <label class="wide">Stream Key<input class="d-key" type="password" value="${esc(d.stream_key || '')}" placeholder="xxxxxxxx"></label>
    <button type="button" class="icon-btn remove" title="حذف" onclick="this.parentElement.remove()">×</button>`;
  $('destinations').appendChild(wrap);
}

function collectPayload() {
  const destinations = [...document.querySelectorAll('.destination')].map(el => ({
    id: el.dataset.id,
    name: el.querySelector('.d-name').value.trim(),
    rtmp_base: el.querySelector('.d-base').value.trim(),
    stream_key: el.querySelector('.d-key').value.trim(),
    enabled: el.querySelector('.d-enabled').checked,
  }));
  return {
    name: $('name').value.trim(),
    source: $('source').value.trim(),
    quality: $('quality').value,
    bitrate: Number($('bitrate').value),
    fps: Number($('fps').value),
    preset: $('preset').value,
    audio_bitrate: 128,
    logo: $('logo').value.trim(),
    logo_width: Number($('logoWidth').value),
    logo_position: $('logoPosition').value,
    text: $('text').value,
    text_size: Number($('textSize').value),
    text_position: $('textPosition').value,
    destinations,
  };
}

$('streamForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    const id = $('streamId').value;
    const payload = collectPayload();
    if (id) await api(`/api/streams/${id}`, {method:'PUT', body:JSON.stringify(payload)});
    else await api('/api/streams', {method:'POST', body:JSON.stringify(payload)});
    closeEditor();
    notify('تم حفظ الإعدادات.');
    await loadStreams();
  } catch (e) { notify(e.message, true); }
});

$('newBtn').addEventListener('click', () => openEditor());

$('logoFile').addEventListener('change', async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append('file', f);
  try {
    const res = await fetch('/api/upload-logo', {method:'POST', body:fd});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Upload failed');
    $('logo').value = data.path;
    notify('تم رفع الشعار.');
  } catch (err) { notify(err.message, true); }
});

async function startStream(id) {
  try { await api(`/api/streams/${id}/start`, {method:'POST'}); notify('بدأ البث.'); await loadStreams(); }
  catch (e) { notify(e.message, true); }
}

async function stopStream(id) {
  try { await api(`/api/streams/${id}/stop`, {method:'POST'}); notify('تم إيقاف البث.'); await loadStreams(); }
  catch (e) { notify(e.message, true); }
}

async function deleteStream(id) {
  if (!confirm('تحذف هذا البث نهائياً؟')) return;
  try { await api(`/api/streams/${id}`, {method:'DELETE'}); await loadStreams(); }
  catch (e) { notify(e.message, true); }
}

async function openLogs(id) {
  const s = streams.find(x => x.id === id);
  $('logTitle').textContent = s?.name || 'Logs';
  $('logModal').classList.remove('hidden');
  async function poll() {
    try {
      const data = await api(`/api/streams/${id}/logs`);
      $('logs').textContent = (data.lines || []).join('\n');
      $('logs').scrollTop = $('logs').scrollHeight;
    } catch (_) {}
  }
  await poll();
  logTimer = setInterval(poll, 1500);
}

function closeLogs() {
  $('logModal').classList.add('hidden');
  if (logTimer) clearInterval(logTimer);
  logTimer = null;
}

setInterval(loadStreams, 5000);
loadStreams();
