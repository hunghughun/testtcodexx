const $ = (id) => document.getElementById(id);
const num = (n, digits = 2) => Number(n).toLocaleString('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits});
const signed = (n) => `${Number(n) >= 0 ? '+' : '−'}${num(Math.abs(Number(n)))}`;
const escaped = (s) => String(s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const time = (s) => new Date(s).toLocaleTimeString('vi-VN', {hour12:false});
const statuses = {idle:'Chưa bắt đầu', running:'Đang mô phỏng', paused:'Đã tạm dừng', stopped:'Đã dừng', target_reached:'Đạt mục tiêu', loss_limit:'Chạm ngưỡng lỗ'};
let hydrated = false, busy = false, latest = null, dirty = false;
const form = $('config-form');
form.addEventListener('input', () => { dirty = true; $('config-help').textContent = 'Có thay đổi chưa lưu. Lưu phiên mới trước khi chạy.'; updateButtons(); });
function error(message) { $('error').textContent = message; $('error').hidden = !message; }
async function api(path, body) {
  const response = await fetch(path, body !== undefined ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {});
  const data = await response.json();
  if (!response.ok) {
    const detail = data.detail;
    throw new Error(Array.isArray(detail) ? detail.map(e => `${e.loc.slice(1).join('.')}: ${e.msg}`).join(' · ') : detail || 'Không thể xử lý yêu cầu.');
  }
  return data;
}
function updateButtons() {
  if (!latest) return;
  const locked = ['running','paused'].includes(latest.status);
  $('start').disabled = busy || dirty || !['idle','paused'].includes(latest.status) || !!latest.error;
  $('start').textContent = latest.status === 'paused' ? 'Tiếp tục mô phỏng' : 'Chạy mô phỏng';
  $('stop').disabled = busy || !locked;
  $('save').disabled = busy || locked;
  for (const input of form.querySelectorAll('input')) input.disabled = busy || locked;
}
function hydrate(config) {
  for (const [key,value] of Object.entries(config)) if (form.elements[key]) form.elements[key].value = Array.isArray(value) ? value.join(', ') : value;
  hydrated = true; dirty = false;
}
function drawChart(state) {
  const history = state.equity_history.length ? state.equity_history : [{tick:0, equity:state.equity}];
  const values = history.map(p => Number(p.equity));
  const min = Math.min(...values), max = Math.max(...values), pad = Math.max((max-min)*.3, 1);
  const low = min-pad, high = max+pad;
  const y = v => 190 - (v-low)/(high-low)*165;
  const x = i => 15 + i/Math.max(1, values.length-1)*600;
  let grid = '';
  for(let i=0;i<4;i++){ const val = low+(high-low)*i/3; grid += `<line x1="15" y1="${y(val)}" x2="620" y2="${y(val)}" stroke="#e2e5e9" stroke-dasharray="3 5"/><text x="635" y="${y(val)+4}" fill="#606975" font-size="10">${num(val)}</text>`; }
  let points = values.map((v,i)=>`${x(i)},${y(v)}`).join(' ');
  if(values.length===1) points += ` 615,${y(values[0])}`;
  const lastX = values.length===1 ? 615 : x(values.length-1);
  $('chart').innerHTML = `${grid}<polyline points="${points}" fill="none" stroke="#315e98" stroke-width="2" stroke-linejoin="round"/><circle cx="${lastX}" cy="${y(values.at(-1))}" r="3" fill="#315e98"/>`;
}
function render(s) {
  latest=s; if(!hydrated) hydrate(s.config);
  $('equity').textContent=num(s.equity); $('target').textContent=num(s.config.target);
  $('floor').textContent=num(Number(s.config.capital)-Number(s.config.max_loss));
  $('max-loss').textContent=`−${num(s.config.max_loss)} USDT`;
  $('pnl').textContent=`${signed(s.pnl)} USDT`; $('pnl').className=Number(s.pnl)>=0?'positive':'negative';
  $('progress').value=Number(s.progress); $('progress-text').textContent=`${num(s.progress,1)}%`;
  $('chart-equity').innerHTML=`${num(s.equity)} <small>USDT</small>`;
  $('leverage-tag').textContent=s.config.leverage; $('status').textContent=statuses[s.status] || s.status;
  const positions=Object.entries(s.positions);
  $('position-count').textContent=positions.length; $('open-count').textContent=positions.length;
  $('position-limit').textContent=`/ ${s.config.symbols.length} cặp`;
  $('used-margin').textContent=`${num(positions.reduce((sum,[,p])=>sum+Number(p.margin),0))} USDT`;
  $('tick-label').textContent=`Phiên #${s.session_id} · ${s.tick} phút giả lập`;
  $('reason').textContent=s.reason;
  $('position-rows').innerHTML=positions.length?positions.map(([symbol,p])=>`<tr><td><strong>${escaped(symbol)}</strong> <span class="side-${p.side.toLowerCase()}">${p.side}</span></td><td>${num(p.entry)}</td><td>${num(s.prices[symbol])}</td><td>${num(p.margin)}</td><td class="negative">${num(p.stop)}</td><td class="positive">${num(p.take)}</td><td class="${Number(p.unrealized_pnl)>=0?'positive':'negative'}">${signed(p.unrealized_pnl)}</td></tr>`).join(''):'<tr><td class="empty" colspan="7"><strong>Chưa có vị thế mở</strong>Bot sẽ chờ dữ liệu và tín hiệu giao cắt SMA trước khi vào lệnh.</td></tr>';
  $('trade-count').textContent=`${s.trades.length} thao tác trong phiên · hiển thị tối đa 50 gần nhất`;
  $('trade-rows').innerHTML=s.trades.length?[...s.trades].reverse().slice(0,50).map(t=>`<tr><td>${time(t.at)}</td><td>${escaped(t.symbol)}</td><td>${t.action==='OPEN'?'Mở':'Đóng'} <span class="side-${t.side.toLowerCase()}">${t.side}</span></td><td class="${Number(t.pnl)>=0?'positive':'negative'}">${t.pnl===null?'—':signed(t.pnl)}</td><td>${escaped(t.reason)}</td></tr>`).join(''):'<tr><td class="empty" colspan="5">Các lệnh mô phỏng sẽ xuất hiện tại đây.</td></tr>';
  $('events').innerHTML=s.events.length?[...s.events].reverse().slice(0,30).map(e=>`<div class="event"><time>${time(e.at)}</time><span>${escaped(e.message)}</span></div>`).join(''):'<p class="empty">Sẵn sàng cho phiên mô phỏng đầu tiên.</p>';
  $('connection').textContent='Đã kết nối · Cập nhật mỗi 2 giây';
  if(s.error) error(s.error);
  if(!dirty) $('config-help').textContent=['running','paused'].includes(s.status)?'Dừng và đóng vị thế trước khi thay đổi cấu hình.':'Phiên cũ được giữ trong lịch sử xuất JSON.';
  updateButtons(); drawChart(s);
}
async function command(path, body={}) {
  busy=true; updateButtons(); error('');
  try { const state=await api(path,body); if(path==='/api/config') hydrate(state.config); render(state); }
  catch(e){error(e.message);} finally{busy=false;updateButtons();}
}
$('start').addEventListener('click',()=>command('/api/start'));
$('stop').addEventListener('click',()=>command('/api/stop'));
form.addEventListener('submit',(e)=>{e.preventDefault(); const data=Object.fromEntries(new FormData(form)); data.symbols=data.symbols.split(',').map(s=>s.trim()).filter(Boolean); data.leverage=Number(data.leverage); command('/api/config',data);});
async function poll(){if(!busy){try{render(await api('/api/state'));}catch(e){$('connection').textContent='Mất kết nối máy chủ';error(e.message);}} setTimeout(poll,2000);}
poll();
