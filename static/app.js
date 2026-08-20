let streams = [];
let logTimer = null;
let lastProbe = null;
let editorWasRunning = false;
let hotApplyTimer = null;
let hotApplyBusy = false;
let hotApplyPending = false;

const $ = (id) => document.getElementById(id);

function notify(msg, error=false) {
  const n = $('notice');
  n.textContent = msg;
  n.className = `notice ${error ? 'error' : 'ok'}`;
  clearTimeout(n._timer);
  n._timer = setTimeout(() => n.classList.add('hidden'), 4500);
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

function makeId() {
  try {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
      return globalThis.crypto.randomUUID().slice(0, 8);
    }
    if (globalThis.crypto && typeof globalThis.crypto.getRandomValues === 'function') {
      const b = new Uint8Array(8);
      globalThis.crypto.getRandomValues(b);
      return [...b].map(x => x.toString(16).padStart(2, '0')).join('').slice(0, 8);
    }
  } catch (_) {}
  return (Date.now().toString(36) + Math.random().toString(36).slice(2, 10)).slice(-8);
}

function esc(s='') {
  return String(s).replace(/[&<>'"]/g, c => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;'
  }[c]));
}

function outputStatus(d) {
  if (!d.enabled) return {label:'OFF', cls:'disabled'};
  const status = d.status || 'stopped';
  if (status === 'running') return {label:'LIVE', cls:'running'};
  if (status === 'reconnecting') return {label:'RECONNECT', cls:'reconnecting'};
  if (status === 'connecting') return {label:'CONNECTING', cls:'connecting'};
  return {label:'STOPPED', cls:'stopped'};
}

async function loadStreams() {
  try {
    streams = await api('/api/streams');
    renderCards();
  } catch (e) {
    notify(e.message, true);
  }
}

function renderCards() {
  const cards = $('cards');
  cards.innerHTML = '';
  $('empty').classList.toggle('hidden', streams.length !== 0);

  for (const s of streams) {
    const enabled = (s.destinations || []).filter(d => d.enabled).length;
    const liveOutputs = (s.destinations || []).filter(d => d.status === 'running').length;
    const card = document.createElement('article');
    card.className = 'stream-card';

    const chips = (s.destinations || []).map(d => {
      const st = outputStatus(d);
      const retries = Number(d.retries || 0);
      const extra = retries ? ` · ↻${retries}` : '';
      return `<span class="chip ${st.cls}" title="${esc(d.last_error || '')}"><i></i>${esc(d.name)} · ${st.label}${extra}</span>`;
    }).join('') || '<span class="chip disabled">لا توجد مخارج</span>';

    card.innerHTML = `
      <div class="card-top">
        <div>
          <div class="status ${s.running ? 'live' : 'off'}"><span></span>${s.running ? 'ENCODER LIVE' : 'STOPPED'}</div>
          <h3>${esc(s.name)}</h3>
          <div class="source">${esc(s.source)}</div>
        </div>
        <button class="icon-btn menu" onclick="editStream('${s.id}')" title="تعديل">✎</button>
      </div>
      <div class="metrics">
        <div><strong>${esc(s.quality)}</strong><span>الجودة</span></div>
        <div><strong>${Number(s.bitrate).toLocaleString()}k</strong><span>Bitrate</span></div>
        <div><strong>${s.fps}</strong><span>FPS</span></div>
        <div><strong>${liveOutputs}/${enabled}</strong><span>Outputs Live</span></div>
      </div>
      <div class="chips">${chips}</div>
      <div class="card-actions">
        ${s.running
          ? `<button class="btn danger grow" onclick="stopStream('${s.id}')">■ إيقاف</button>`
          : `<button class="btn primary grow" onclick="startStream('${s.id}')">▶ تشغيل</button>`}
        <button class="btn ghost" onclick="editStream('${s.id}')">⚙ تعديل</button>
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
  $('probeResult').classList.add('hidden');
  $('hotEditBanner').classList.add('hidden');
  $('hotState').textContent = 'جاهز';
  lastProbe = null;
  editorWasRunning = false;
  hotApplyBusy = false;
  hotApplyPending = false;
  clearTimeout(hotApplyTimer);
  setProcessingLocked(false);
  showLogoPreview('');
  addDestination({}, false);
}

function setProcessingLocked(locked) {
  const ids = [
    'source','quality','bitrate','fps','preset','logo','logoFile','logoWidth','logoPosition',
    'text','textSize','textPosition'
  ];
  ids.forEach(id => {
    const el = $(id);
    if (el) el.disabled = !!locked;
  });
  $('applyProbeBtn').disabled = !!locked;
  document.querySelectorAll('.panel.soft').forEach(p => p.classList.toggle('locked-panel', !!locked));
}

function openEditor(stream=null) {
  resetForm();
  if (stream) {
    editorWasRunning = !!stream.running;
    $('modalTitle').textContent = stream.running ? 'تعديل المخارج أثناء البث' : 'تعديل البث';
    $('streamId').value = stream.id;
    $('name').value = stream.name || '';
    $('source').value = stream.source || '';
    $('quality').value = stream.quality || '1080p';
    $('bitrate').value = stream.bitrate || 5000;
    ensureSelectOption($('fps'), Number(stream.fps || 50));
    $('fps').value = String(stream.fps || 50);
    $('preset').value = stream.preset || 'superfast';
    $('logo').value = stream.logo || '';
    $('logoWidth').value = stream.logo_width || 335;
    $('logoPosition').value = stream.logo_position || 'top-right';
    $('text').value = stream.text || '';
    $('textSize').value = stream.text_size || 38;
    $('textPosition').value = stream.text_position || 'bottom-center';
    $('destinations').innerHTML = '';
    (stream.destinations || []).forEach(d => addDestination(d, false));
    if (!(stream.destinations || []).length) addDestination({}, false);
    showLogoPreview(stream.logo || '');

    if (stream.running) {
      $('hotEditBanner').classList.remove('hidden');
      setProcessingLocked(true);
    }
  } else {
    $('modalTitle').textContent = 'بث جديد';
  }
  $('modal').classList.remove('hidden');
}

function closeEditor() {
  clearTimeout(hotApplyTimer);
  $('modal').classList.add('hidden');
}

function editStream(id) {
  const s = streams.find(x => x.id === id);
  if (s) openEditor(s);
}

function addDestination(d={}, userInitiated=true) {
  const wrap = document.createElement('div');
  wrap.className = 'destination';
  wrap.dataset.id = d.id || makeId();
  const st = outputStatus(d);
  const runtime = d.id
    ? `<div class="d-runtime ${st.cls}"><i></i>${st.label}${d.retries ? ` · Retry ${d.retries}` : ''}</div>`
    : '<div class="d-runtime stopped"><i></i>NEW</div>';

  wrap.innerHTML = `
    <div class="dest-state">
      <label class="check"><input type="checkbox" class="d-enabled" ${d.enabled === false ? '' : 'checked'}><span>فعال</span></label>
      ${runtime}
    </div>
    <label>الاسم<input class="d-name" value="${esc(d.name || 'Telegram')}" placeholder="Telegram 1"></label>
    <label class="wide">RTMP / RTMPS Server<input class="d-base" value="${esc(d.rtmp_base || 'rtmps://dc4-1.rtmp.t.me/s/')}" placeholder="rtmps://.../s/"></label>
    <label class="wide">Stream Key<input class="d-key" type="password" value="${esc(d.stream_key || '')}" placeholder="xxxxxxxx"></label>
    <div class="dest-buttons">
      ${editorWasRunning && d.id ? `<button type="button" class="icon-btn reconnect" title="Reconnect هذا المخرج">↻</button>` : ''}
      <button type="button" class="icon-btn remove" title="حذف هذا المخرج">×</button>
    </div>`;

  const removeBtn = wrap.querySelector('.remove');
  removeBtn.addEventListener('click', () => {
    wrap.remove();
    if (editorWasRunning) scheduleHotApply(100);
  });

  const reconnectBtn = wrap.querySelector('.reconnect');
  if (reconnectBtn) {
    reconnectBtn.addEventListener('click', async () => {
      const sid = $('streamId').value;
      try {
        reconnectBtn.disabled = true;
        await api(`/api/streams/${sid}/outputs/${wrap.dataset.id}/reconnect`, {method:'POST'});
        notify('تم إعادة تشغيل هذا الـOutput فقط.');
        await loadStreams();
      } catch (e) {
        notify(e.message, true);
      } finally {
        reconnectBtn.disabled = false;
      }
    });
  }

  wrap.addEventListener('input', () => {
    if (editorWasRunning) scheduleHotApply(800);
  });
  wrap.addEventListener('change', () => {
    if (editorWasRunning) scheduleHotApply(150);
  });

  $('destinations').appendChild(wrap);
  if (userInitiated && editorWasRunning) scheduleHotApply(100);
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

function scheduleHotApply(delay=600) {
  if (!editorWasRunning || !$('streamId').value) return;
  clearTimeout(hotApplyTimer);
  $('hotState').textContent = 'بانتظار التطبيق…';
  hotApplyTimer = setTimeout(() => hotApplyOutputs(), delay);
}

async function hotApplyOutputs() {
  if (!editorWasRunning || !$('streamId').value) return;
  if (hotApplyBusy) {
    hotApplyPending = true;
    return;
  }

  hotApplyBusy = true;
  $('hotState').textContent = 'جاري التطبيق…';
  try {
    const id = $('streamId').value;
    await api(`/api/streams/${id}`, {method:'PUT', body:JSON.stringify(collectPayload())});
    $('hotState').textContent = '✓ مطبق مباشرة';
    await loadStreams();
  } catch (e) {
    $('hotState').textContent = 'فشل التطبيق';
    notify(e.message, true);
  } finally {
    hotApplyBusy = false;
    if (hotApplyPending) {
      hotApplyPending = false;
      scheduleHotApply(150);
    }
  }
}

$('streamForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    clearTimeout(hotApplyTimer);
    const id = $('streamId').value;
    const payload = collectPayload();
    if (id) await api(`/api/streams/${id}`, {method:'PUT', body:JSON.stringify(payload)});
    else await api('/api/streams', {method:'POST', body:JSON.stringify(payload)});
    closeEditor();
    notify(editorWasRunning ? 'تم تطبيق المخارج بدون إعادة تشغيل الـEncoder.' : 'تم حفظ الإعدادات.');
    await loadStreams();
  } catch (e) {
    notify(e.message, true);
  }
});

$('newBtn').addEventListener('click', () => openEditor());

function logoToUrl(value) {
  const v = String(value || '').trim();
  if (!v) return '';
  if (/^https?:\/\//i.test(v) || v.startsWith('/uploads/')) return v;
  const marker = '/uploads/';
  const idx = v.lastIndexOf(marker);
  if (idx >= 0) return '/uploads/' + encodeURIComponent(v.slice(idx + marker.length));
  return '';
}

function showLogoPreview(value, explicitUrl='') {
  const url = explicitUrl || logoToUrl(value);
  const wrap = $('logoPreviewWrap');
  const img = $('logoPreview');
  if (!url) {
    wrap.classList.add('hidden');
    img.removeAttribute('src');
    return;
  }
  img.src = url;
  img.onload = () => wrap.classList.remove('hidden');
  img.onerror = () => wrap.classList.add('hidden');
}

$('logo').addEventListener('input', (e) => showLogoPreview(e.target.value));

$('clearLogoBtn').addEventListener('click', () => {
  $('logo').value = '';
  $('logoFile').value = '';
  showLogoPreview('');
});

$('logoFile').addEventListener('change', async (e) => {
  const f = e.target.files[0];
  if (!f) return;

  const localUrl = URL.createObjectURL(f);
  showLogoPreview('', localUrl);

  const fd = new FormData();
  fd.append('file', f);
  try {
    const res = await fetch('/api/upload-logo', {method:'POST', body:fd});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Upload failed');
    $('logo').value = data.path;
    showLogoPreview(data.path, data.url);
    notify('تم رفع الصورة وظهرت المعاينة.');
  } catch (err) {
    notify(err.message, true);
  } finally {
    setTimeout(() => URL.revokeObjectURL(localUrl), 1000);
  }
});

function ensureSelectOption(select, value) {
  if (!select || value === null || value === undefined || Number.isNaN(Number(value))) return;
  const str = String(Math.round(Number(value)));
  if (![...select.options].some(o => o.value === str)) {
    const o = document.createElement('option');
    o.value = str;
    o.textContent = str;
    select.appendChild(o);
  }
}

function setProbeResult(data) {
  lastProbe = data;
  const v = data.video || {};
  const a = data.audio || {};
  $('probeResolution').textContent = v.resolution || 'غير معروف';
  $('probeFps').textContent = v.fps ?? 'غير معروف';
  $('probeCodec').textContent = (v.codec || 'unknown').toUpperCase();
  $('probeBitrate').textContent = v.bitrate_kbps ? `${Number(v.bitrate_kbps).toLocaleString()} kbps` : 'غير متاح';
  const audioParts = [a.codec && a.codec !== 'none' ? a.codec.toUpperCase() : 'No audio'];
  if (a.sample_rate) audioParts.push(`${a.sample_rate}Hz`);
  if (a.channels) audioParts.push(`${a.channels}ch`);
  $('probeAudio').textContent = audioParts.join(' · ');
  $('probeFormat').textContent = data.format?.name ? `Format: ${data.format.name}` : '';
  $('probeResult').classList.remove('hidden');
}

async function probeCurrentSource() {
  const source = $('source').value.trim();
  if (!source) return notify('حط رابط المصدر أولاً.', true);
  const btn = $('probeBtn');
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'جاري الفحص…';
  try {
    const data = await api('/api/probe-source', {method:'POST', body:JSON.stringify({source})});
    setProbeResult(data);
    notify('تم فحص المصدر.');
  } catch (e) {
    notify(`فشل الفحص: ${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

$('probeBtn').addEventListener('click', probeCurrentSource);

$('applyProbeBtn').addEventListener('click', () => {
  if (!lastProbe) return;
  if (editorWasRunning) return notify('أوقف البث أولاً لتغيير إعدادات المعالجة.', true);
  const r = lastProbe.recommended || {};
  if (r.quality) $('quality').value = r.quality;
  if (r.fps) {
    ensureSelectOption($('fps'), r.fps);
    $('fps').value = String(Math.round(Number(r.fps)));
  }
  if (r.bitrate && Number(r.bitrate) >= 300 && Number(r.bitrate) <= 30000) {
    $('bitrate').value = Math.round(Number(r.bitrate));
  }
  notify('تم نسخ الجودة وFPS والـBitrate المتاح إلى المعالجة.');
});

async function startStream(id) {
  try {
    await api(`/api/streams/${id}/start`, {method:'POST'});
    notify('بدأ الـEncoder والمخارج.');
    await loadStreams();
  } catch (e) {
    notify(e.message, true);
  }
}

async function stopStream(id) {
  try {
    await api(`/api/streams/${id}/stop`, {method:'POST'});
    notify('تم إيقاف البث.');
    await loadStreams();
  } catch (e) {
    notify(e.message, true);
  }
}

async function deleteStream(id) {
  if (!confirm('تحذف هذا البث نهائياً؟')) return;
  try {
    await api(`/api/streams/${id}`, {method:'DELETE'});
    await loadStreams();
  } catch (e) {
    notify(e.message, true);
  }
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

setInterval(loadStreams, 3000);
loadStreams();
