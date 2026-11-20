const msg = document.getElementById('msg');
const view = document.getElementById('view');
let windows = [];

async function api(path, opts={}) {
  const r = await fetch(path, { headers:{'Content-Type':'application/json'}, ...opts });
  const j = await r.json().catch(()=>({}));
  if (!r.ok) {
    const e = new Error(j.error || ('HTTP '+r.status));
    e.payload = j;
    throw e;
  }
  return j;
}
function setMsg(t, err=false){ msg.textContent=t||''; msg.className = err?'err':'muted'; }
function esc(v){ return String(v ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function chip(text){ return `<span class="tag ${text==='拒签'?'chip-reject':''}">${esc(text)}</span>`; }
async function loadWindows(){ windows = (await api('/api/windows')).items || []; }

function renderWindows(){
  view.innerHTML = `<div class="wall">${windows.map(w=>`
    <article class="card">
      <h3>${esc(w.table_name)}</h3>
      <p class="muted">${esc(w.start_date)} → ${esc(w.end_date)}</p>
      <span class="tag">${esc(w.status)}</span>
      <p class="muted">切片 ${w.slice_count} · 已落库 ${w.loaded_count}</p>
    </article>`).join('')}</div>`;
}

function winOptions(){ return windows.map(w=>`<option value="${w.id}">#${w.id} ${esc(w.table_name)}</option>`).join(''); }

function renderPlan(){
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">生成切片计划</button>
    <pre id="out" class="muted"></pre>
  </div>`;
  document.getElementById('go').onclick = async ()=>{
    try{
      const wid = document.getElementById('wid').value;
      const j = await api(`/api/windows/${wid}/plan`, {method:'POST', body:'{}'});
      document.getElementById('out').textContent = JSON.stringify(j.slices, null, 2);
      setMsg(`已生成 ${j.slices.length} 片`);
      await loadWindows();
    }catch(e){ setMsg(e.message,true); }
  };
}

async function renderReplay(){
  await loadWindows();
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <div id="list" class="wall" style="margin-top:0.8rem"></div>
  </div>`;
  const draw = async ()=>{
    const id = document.getElementById('wid').value;
    const items = (await api(`/api/windows/${id}/slices`)).items || [];
    document.getElementById('list').innerHTML = items.map(s=>`
      <article class="card">
        <h3>${esc(s.slice_date)}</h3>
        <span class="tag">${esc(s.status)}</span>
        <p class="muted">行数 ${s.row_count}</p>
        <button data-id="${s.id}" class="primary" type="button" ${s.status!=='待重放'?'disabled':''}>重放</button>
      </article>`).join('') || '<p class="muted">先生成切片计划</p>';
    document.querySelectorAll('#list button[data-id]').forEach(btn=>{
      btn.onclick = async ()=>{
        try{
          const j = await api(`/api/slices/${btn.dataset.id}/replay`, {method:'POST', body:'{}'});
          setMsg(`已落库 ${j.slice.slice_date} · ${j.slice.row_count} 行`);
          await draw(); await loadWindows();
        }catch(e){ setMsg(e.message,true); }
      };
    });
  };
  document.getElementById('wid').onchange = draw;
  await draw();
}

async function renderCoverage(){
  await loadWindows();
  const items = (await api('/api/coverage-reports')).items || [];
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">跑覆盖对账</button>
    <div class="wall" style="margin-top:0.8rem">${items.map(r=>`
      <article class="card"><h3>窗口 #${r.window_id}</h3><p>${esc(r.summary)}</p></article>`).join('')}</div>
  </div>`;
  document.getElementById('go').onclick = async ()=>{
    try{
      const j = await api('/api/coverage-reports', {method:'POST', body: JSON.stringify({window_id: Number(document.getElementById('wid').value)})});
      setMsg(j.report.summary);
      await renderCoverage();
    }catch(e){ setMsg(e.message,true); }
  };
}

async function loadPanels(){
  const j = await api('/api/daycut/panels');
  j.attach = j.attach || {};
  j.attach.cross = j.attach.cross || [];
  j.attach.rejected = j.attach.rejected || [];
  j.attach.mismatched = j.attach.mismatched || [];
  j.attach.blocked_keys = j.attach.blocked_keys || [];
  j.attach.plan = j.attach.plan || {};
  j.attach.trace = j.attach.trace || [];
  j.attach.steps = j.attach.steps || [];
  return j;
}

function crossBanner(j){
  const b = j.bound || {};
  if (b.consistent) return '';
  return `<p class="banner err" data-cross>切点双界：配置展示 ${esc(b.display)} / 现行调度 ${esc(b.live)} / 同源切点 ${esc(b.cut_point)} —— 片归属按自然日，末日行不并入次日；落点已拒签，先纠偏到 00:00</p>`;
}

/* 串日队列：亮拒签芯片，并标记拒签/跨日片 */
async function render_queue(){
  const j = await loadPanels();
  const plans = j.plan || {};
  view.innerHTML = `${crossBanner(j)}
      <p>${chip(j.queue_chip)} <span class="muted">串日队列 · 拒签 ${j.rejected_count||0} 条 · 切点 v${esc(j.bound.version)}</span></p>
    <div class="wall">${(j.queue||[]).map(r=>`
      <article class="card ${r.status==='拒签'?'is-reject':''}">
        <h3>${esc(r.biz_date)}</h3>
        <p class="muted">${esc(r.row_key)} · 事件日 ${esc(r.event_date||r.biz_date)}</p>
        <p>${chip(r.chip)} ${r.cross_day?'<span class="tag">跨日</span>':''} <span class="tag">${esc(r.status)}</span></p>
        ${r.reason?`<p class="muted">${esc(r.reason)}</p>`:''}
      </article>`).join('')||'<p class="muted">队列为空</p>'}</div>
    <article class="card">
      <label>落点键<input id="rk" value="eod-1" /></label>
      <label>事件时间<input id="ts" value="2026-11-10T23:30:00" /></label>
      <button class="primary" id="p" type="button">日界落点放行</button>
      <button id="c" type="button">纠偏</button>
      <p class="muted">计划片键：${esc(JSON.stringify(plans))}</p>
    </article></div>`;
  document.getElementById('p').onclick = async ()=>{
    try{
      const r = await api('/api/daycut/place',{method:'POST',body: JSON.stringify({
        row_key: document.getElementById('rk').value,
        event_ts: document.getElementById('ts').value,
      })});
      setMsg(`落入 ${r.biz_date} · ${r.chip}`);
      await render_queue();
    }catch(e){ setMsg(e.message,true); await render_queue(); }
  };
  document.getElementById('c').onclick = async ()=>{
    try{
      const r = await api('/api/daycut/correct',{method:'POST',body: JSON.stringify({
        expected_version: j.bound.version,
      })});
      setMsg(`${r.note}（v${r.version}）`);
      await render_queue();
    }catch(e){ setMsg(e.message,true); await render_queue(); }
  };
}

/* 日界详情：三个切点同屏 */
async function render_bound(){
  const j = await loadPanels();
  const b = j.bound || {};
  view.innerHTML = `${crossBanner(j)}
    <div class="card">
      <p>${chip(b.chip)} <span class="muted">切点版本 v${esc(b.version)}</span></p>
      <div class="wall">
        <article class="card ${b.display!=='00:00'?'is-reject':''}"><h3>配置展示</h3><p>${esc(b.display)}</p></article>
        <article class="card ${b.live!=='00:00'?'is-reject':''}"><h3>现行调度</h3><p>${esc(b.live)}</p></article>
        <article class="card ${b.cut_point!=='00:00'?'is-reject':''}"><h3>同源切点</h3><p>${esc(b.cut_point)}</p></article>
        <article class="card"><h3>片归属</h3><p>${esc(b.belong||'自然日')}</p></article>
      </div>
      <p class="muted">${b.consistent?'三处切点都收拢到 00:00，片归属按自然日可写库':'任一不是 00:00 即双界：末日行绝不并入次日，落点、计划、重放一律拒签'}</p>
    </div>`;
}

/* 切点策略：拒签芯片与处置方式 */
async function render_strategy(){
  const j = await loadPanels();
  const s = j.strategy || {};
  view.innerHTML = `${crossBanner(j)}
    <div class="card">
      <p>${chip(s.chip)} <span class="muted">处置结局 · 策略版本 v${esc(s.version)} · 当前 ${esc(s.health_chip||'')}</span></p>
      <h3>${esc(s.fate)}</h3>
      <p class="muted">串日脏片处置：${esc(s.disposition)}</p>
      <p class="muted">${esc(s.detail)}</p>
      <label>纠偏目标切点<input id="cp" value="00:00" /></label>
      <button class="primary" id="c" type="button">收拢到 00:00</button>
    </div>`;
  document.getElementById('c').onclick = async ()=>{
    try{
      const r = await api('/api/daycut/correct',{method:'POST',body: JSON.stringify({
        cut_point: document.getElementById('cp').value,
        expected_version: j.bound.version,
      })});
      setMsg(`${r.note}（v${r.version}）`);
      await render_strategy();
    }catch(e){ setMsg(e.message,true); await render_strategy(); }
  };
}

/* 对账附页：复验结论 + 拒签样本 + 次日计划 + 编排留痕 */
async function render_attach(){
  const j = await loadPanels();
  const a = j.attach;
  view.innerHTML = `${crossBanner(j)}
    <div class="card">
      <p>${chip(a.chip)} <strong>${esc(a.summary)}</strong>
        <span class="muted">当前 ${esc(a.health_chip||'')}</span></p>
      <p class="muted">串日样本 ${a.cross.length} · 归属不符 ${a.mismatched.length} · 拒签 ${a.rejected.length}</p>
      <h4>挡在计划外的串日片键</h4>
      <pre class="muted">${esc(a.blocked_keys.length ? a.blocked_keys.join('、') : '无')}</pre>
      <h4>计划片键（按归属日）</h4>
      <pre class="muted">${esc(JSON.stringify(a.plan, null, 2))}</pre>
      <h4>复验步骤</h4>
      <ol class="muted">${a.steps.map(s=>`<li>${esc(s)}</li>`).join('')}</ol>
      <h4>编排留痕</h4>
      <ul class="muted">${a.trace.map(t=>`<li>[${esc(t.chip)}] ${esc(t.action)} · ${esc(t.detail)}</li>`).join('')||'<li>暂无</li>'}</ul>
    </div>`;
}

async function show(tab){
  setMsg(''); await loadWindows();
  if (tab==='queue') await render_queue();
  if (tab==='bound') await render_bound();
  if (tab==='strategy') await render_strategy();
  if (tab==='attach') await render_attach();
}
document.querySelectorAll('.chips button').forEach(btn=>{
  btn.onclick=()=>{ document.querySelectorAll('.chips button').forEach(b=>b.classList.remove('on')); btn.classList.add('on'); show(btn.dataset.tab); };
});
document.getElementById('btn-logout').onclick = async ()=>{ await api('/api/logout',{method:'POST',body:'{}'}); location.href='/login'; };
show('queue');
