const el=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const format=n=>Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4});
const labels={idle:'Chưa bắt đầu',running:'Đang chạy Demo',paused:'Demo đã tạm dừng',stopping:'Đang đóng và đối soát',stopped:'Demo đã dừng',target_reached:'Đạt mục tiêu phiên Demo',loss_limit:'Chạm ngưỡng lỗ'};
let state=null,busy=false,hydrated=false,dirty=false;
const configForm=el('demo-config');
configForm.addEventListener('input',()=>{dirty=true;});
function showError(message){el('demo-error').textContent=message;el('demo-error').hidden=!message;}
async function api(path,body){
  const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await response.json().catch(()=>({detail:'Máy chủ chưa trả lời đúng định dạng; hãy kiểm tra ứng dụng.'}));
  if(!response.ok)throw new Error(Array.isArray(data.detail)?data.detail.map(x=>x.msg).join(' · '):data.detail||'Không thực hiện được yêu cầu.');
  return data;
}
function controls(){
  const active=state&&['running','paused','stopping'].includes(state.status);
  const connected=state?.connected;
  el('demo-start').disabled=busy||!connected||active;
  el('demo-connect').disabled=busy||state?.status==='running';
  el('demo-refresh').disabled=busy||!connected;
  el('demo-stop').disabled=busy||!connected||!active;
  el('demo-resume').disabled=busy||!connected||state?.status!=='paused';
  const manual=connected&&state.status==='running'&&state.config.strategy==='manual'&&!state.waiting;
  el('demo-long').disabled=el('demo-short').disabled=busy||!manual;
  for(const input of configForm.querySelectorAll('input,select'))input.disabled=busy||active;
}
function render(s){
  state=s;
  if(!hydrated||(!dirty&&!['idle','stopped','target_reached','loss_limit'].includes(s.status))){
    for(const [key,value] of Object.entries(s.config))if(configForm.elements[key])configForm.elements[key].value=Array.isArray(value)?value.join(', '):value;
    hydrated=true;
  }
  el('demo-status').textContent=labels[s.status]||s.status;
  el('demo-connection').textContent=s.connected?'Đã kết nối Binance Demo':'Chưa kết nối';
  el('demo-equity').textContent=format(s.equity);
  el('demo-pnl').textContent=`Lãi/lỗ đã chốt sau phí và funding: ${format(s.net)} USDT`;
  el('demo-available').textContent=s.account?format(s.account.available):'—';
  el('demo-count').textContent=Object.keys(s.positions).length;
  el('demo-updated').textContent=s.account?`Đối soát: ${new Date(s.account.checked_at).toLocaleTimeString('vi-VN')}`:'Chưa cập nhật';
  el('demo-sync').textContent=s.waiting?'Đang chờ lịch sử khớp/chi phí đồng bộ; chưa mở lệnh mới.':s.error||'';
  const selected=el('demo-symbol').value;
  el('demo-symbol').innerHTML=s.config.symbols.map(x=>`<option>${esc(x)}</option>`).join('');
  if(s.config.symbols.includes(selected))el('demo-symbol').value=selected;
  el('demo-positions').innerHTML=Object.entries(s.positions).map(([symbol,p])=>{
    const b=s.brackets[symbol]||{};
    const protect=['sl','tp'].map(k=>{const o=s.operations[b[k]];return o?`${k.toUpperCase()}: ${esc(o.params.triggerPrice)} (${esc(o.status)})`:`${k.toUpperCase()}: chưa xác nhận`;}).join('<br>');
    return `<tr><td>${esc(symbol)}</td><td>${esc(p.side)}</td><td>${esc(p.quantity)}</td><td>${format(p.entry)} / ${format(p.mark)}</td><td>${format(p.unrealized)}</td><td>${protect}</td></tr>`;
  }).join('')||'<tr><td colspan="6" class="empty">Chưa có vị thế của bot đã được đối soát.</td></tr>';
  el('demo-orders').innerHTML=Object.values(s.operations).reverse().slice(0,50).map(o=>`<tr><td>${esc(o.client_id)}</td><td>${esc(o.symbol)} / ${esc(o.kind)}</td><td>${esc(o.status)}</td></tr>`).join('')||'<tr><td colspan="3" class="empty">Chưa gửi lệnh Demo.</td></tr>';
  el('demo-events').innerHTML=[...s.events].reverse().slice(0,30).map(e=>`<div class="event"><time>${new Date(e.at).toLocaleTimeString('vi-VN')}</time><span>${esc(e.message)}</span></div>`).join('');
  controls();
}
async function command(path,body={}){
  busy=true;controls();showError('');
  try{const s=await api(path,body);if(path.endsWith('/start'))dirty=false;render(s);}
  catch(e){showError(e.message);try{render(await api('/api/demo/state'));}catch{}}
  finally{busy=false;controls();}
}
el('credentials-form').addEventListener('submit',async e=>{
  e.preventDefault();const form=e.currentTarget;
  const body={api_key:form.elements.api_key.value,api_secret:form.elements.api_secret.value};
  form.reset();await command('/api/demo/connect',body);body.api_key='';body.api_secret='';
});
configForm.addEventListener('submit',e=>{
  e.preventDefault();const config=Object.fromEntries(new FormData(configForm));
  config.symbols=config.symbols.split(',').map(s=>s.trim().toUpperCase()).filter(Boolean);config.leverage=Number(config.leverage);
  command('/api/demo/start',{config,confirm_demo:el('confirm-demo').checked});
});
for(const [id,path] of [['demo-refresh','refresh'],['demo-stop','stop'],['demo-resume','resume']])el(id).addEventListener('click',()=>command('/api/demo/'+path));
for(const side of ['LONG','SHORT'])el('demo-'+side.toLowerCase()).addEventListener('click',()=>command('/api/demo/order',{symbol:el('demo-symbol').value,side,request_id:crypto.randomUUID()}));
async function poll(){if(!busy){try{render(await api('/api/demo/state'));}catch(e){showError(e.message);}}setTimeout(poll,3000);}
poll();
