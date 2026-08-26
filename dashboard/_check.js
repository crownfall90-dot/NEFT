
const $ = id => document.getElementById(id);
function setText(id,t){ const e=$(id); if(e) e.textContent=t; }
function setVal(id,v){ const e=$(id); if(e) e.value=v; }
function setChk(id,v){ const e=$(id); if(e) e.checked=!!v; }
function toast(t){ const e=$("toast"); e.textContent=t; e.style.display="block"; setTimeout(()=>e.style.display="none",1600); }
function money(x){ return (x??0).toLocaleString("en-US",{maximumFractionDigits:2}); }
function n(x){ return Number(x).toLocaleString("en-US",{maximumSignificantDigits:7}); }
function pair(v){ if(Array.isArray(v)) return v[0]+"-"+v[1]; return v||""; }
function parsePair(s){ const p=String(s).replace("–","-").split("-"); return p.length===2?[+p[0],+p[1]]:s; }
function shortStatus(s){
  s=String(s||"");
  if(s.includes("позиц")) return "в сделке";
  if(s.includes("ордер")) return "ордер";
  return "ждёт";
}

let S={}, UNI=[], STARTER=[];
function editing(){
  const a=document.activeElement;
  return !!(a && (a.tagName==="INPUT" || a.tagName==="SELECT" || a.tagName==="TEXTAREA"));
}
function coinBoxes(selected){
  const on=new Set(selected||[]);
  const coins=[...new Set([...(UNI.length?UNI:[]), ...(S.crypto_symbols||[]), ...Object.keys(S.crypto_routes||{})])];
  return coins.filter(s=>!String(s).startsWith("BTC")).map(s=>
    `<label class="chk"><input type="checkbox" data-c="${s}" ${on.has(s)?"checked":""}> ${s.split("/")[0]}</label>`).join("");
}
function fill(cfg){
  if(!cfg) return;
  S=cfg;
  $("deposit").value=cfg.deposit; $("risk_pct").value=cfg.risk_pct;
  $("venue").value=cfg.venue; $("leverage_crypto").value=cfg.leverage_crypto;
  $("leverage_mt5").value=cfg.leverage_mt5;
  $("max_daily_loss_pct").value=cfg.max_daily_loss_pct;
  $("max_drawdown_pct").value=cfg.max_drawdown_pct;
  $("max_open_positions").value=cfg.max_open_positions??1;
  $("interval_sec").value=cfg.interval_sec??5;
  $("exchange").value=cfg.exchange;
  $("allow_live").checked=!!cfg.allow_live;
  const rk=$("riskK"); if(rk) rk.textContent=(cfg.risk_pct??0)+"%";
  const st=cfg.strategies||{};
  const h=st.hss||{}, l=st.london_sr||{}, b=st.breakout||{}, q=st.squeeze||{};
  const f=st.session_flow||{}, pb=st.playbook||{};
  $("hss_on").checked=h.enabled; $("hss_tf").value=h.tf; $("hss_rr").value=h.rr;
  $("hss_pb").value=h.pullback; $("hss_sess").value=pair(h.session); $("hss_24").checked=!!h.all_day;
  $("lsr_on").checked=l.enabled; $("lsr_tf").value=l.tf; $("lsr_rr").value=l.min_rr;
  $("lsr_lon").value=pair(l.london); $("lsr_ny").value=pair(l.ny);
  $("bo_on").checked=b.enabled; $("bo_tf").value=b.tf; $("bo_rr").value=b.rr;
  $("sq_on").checked=q.enabled; $("sq_tf").value=q.tf; $("sq_rr").value=q.min_rr;
  $("flow_on").checked=!!f.enabled; $("flow_tf").value=f.tf||"5m"; $("flow_rr").value=f.rr||1;
  $("pb_on").checked=!!pb.enabled;
  $("news_gate").checked=(cfg.news||{}).gate!==false; $("news_buf").value=(cfg.news||{}).buffer_min||5;
  const sc=cfg.scanner||{};
  $("scan_on").checked=sc.enabled!==false;
  $("scan_k").value=sc.top_k||2;
  $("scan_min").value=sc.min_score||1.2;
  $("mt5_symbols").value=(cfg.mt5_symbols||[]).join(", ");
  $("crypto_list").innerHTML=coinBoxes(cfg.crypto_symbols||[]);
}
function collect(){
  let crypto=[...document.querySelectorAll("#crypto_list [data-c]:checked")].map(x=>x.dataset.c);
  if(!crypto.length) crypto=S.crypto_symbols||[];
  return {
    deposit:+$("deposit").value, risk_pct:+$("risk_pct").value, venue:$("venue").value,
    leverage_crypto:+$("leverage_crypto").value, leverage_mt5:+$("leverage_mt5").value,
    max_daily_loss_pct:+$("max_daily_loss_pct").value, max_drawdown_pct:+$("max_drawdown_pct").value,
    max_open_positions:+$("max_open_positions").value, interval_sec:+$("interval_sec").value,
    exchange:$("exchange").value, allow_live:$("allow_live").checked,
    mt5_symbols:$("mt5_symbols").value.split(",").map(s=>s.trim()).filter(Boolean),
    crypto_symbols:crypto,
    news:{ gate:$("news_gate").checked, buffer_min:+$("news_buf").value, high_only:true },
    scanner:{ enabled:$("scan_on").checked, top_k:+$("scan_k").value, min_score:+$("scan_min").value },
    strategies:{
      hss:{ enabled:$("hss_on").checked, tf:$("hss_tf").value, rr:+$("hss_rr").value,
        pullback:+$("hss_pb").value, session:parsePair($("hss_sess").value), all_day:$("hss_24").checked },
      london_sr:{ enabled:$("lsr_on").checked, tf:$("lsr_tf").value, min_rr:+$("lsr_rr").value,
        london:parsePair($("lsr_lon").value), ny:parsePair($("lsr_ny").value) },
      breakout:{ enabled:$("bo_on").checked, tf:$("bo_tf").value, rr:+$("bo_rr").value },
      squeeze:{ enabled:$("sq_on").checked, tf:$("sq_tf").value, min_rr:+$("sq_rr").value },
      session_flow:{ enabled:$("flow_on").checked, tf:$("flow_tf").value, rr:+$("flow_rr").value,
        setups:"blend", gate_regime:true },
      playbook:{ enabled:$("pb_on").checked, tf:"5m" },
    }
  };
}
const sheets={
  account:()=>`<div class="grid">
    <div class="card"><label>депозит $</label><input id="d_deposit" type="number" value="${$("deposit").value}"></div>
    <div class="card"><label>риск на сделку %</label><input id="d_risk" type="number" step="0.1" min="0.1" max="5" value="${$("risk_pct").value}"></div>
    <div class="card"><label>рынок</label><select id="d_venue">${$("venue").innerHTML}</select></div>
    <div class="card"><label>биржа</label><select id="d_ex">${$("exchange").innerHTML}</select></div>
    <div class="card"><label>плечо крипта</label><input id="d_lc" type="number" min="1" max="20" value="${$("leverage_crypto").value}"></div>
    <div class="card"><label>плечо MT5</label><input id="d_lm" type="number" value="${$("leverage_mt5").value}"></div>
    <div class="card"><label>стоп дня %</label><input id="d_ddl" type="number" step="0.5" value="${$("max_daily_loss_pct").value}"></div>
    <div class="card"><label>выкл. при DD %</label><input id="d_dd" type="number" step="0.5" value="${$("max_drawdown_pct").value}"></div>
    <div class="card"><label>макс. позиций</label><input id="d_pos" type="number" min="1" max="8" value="${$("max_open_positions").value}"></div>
    <div class="card"><label>интервал сек</label><input id="d_int" type="number" min="3" max="60" value="${$("interval_sec").value}"></div>
  </div>
  <p class="hint" style="margin-top:12px">Всё это ваше: риск, стопы, плечо, число позиций. Рекомендуемый старт 0.5% / 1 позиция / стоп дня 4%. BTC в боте нельзя включить. Сохраните, потом «Тест» / «Демо» / «Бой».</p>
  <label class="chk"><input type="checkbox" id="d_allow" ${$("allow_live").checked?"checked":""}> разрешить бой</label>
  <input id="d_confirm" placeholder="LIVE" value="${$("confirm").value}">
  <p><button class="btn b-dim" onclick="pullAccount();save()">Сохранить</button>
     <button class="btn b-bad" onclick="pullAccount();runLive()">Бой</button></p>`,
  strat:()=>`<div class="card" style="margin-bottom:12px">
    <h4>Сканер</h4>
    <p class="hint">Смотрит только отмеченные в «Пары» монеты. Выкл — сигналы со всех выбранных пар.</p>
    <label class="chk"><input type="checkbox" id="d_scan_on" ${$("scan_on").checked?"checked":""}> сканер включён</label>
    <label>монет в работе</label><input id="d_scan_k" type="number" min="1" max="8" value="${$("scan_k").value}">
    <label>мин. score</label><input id="d_scan_min" type="number" step="0.1" value="${$("scan_min").value}">
    <p class="hint" id="d_scan_now"></p>
  </div>
  <p class="hint">Галочка «вкл» — стратегия торгует. Сняли — бот её больше не берёт (на следующем цикле, без рестарта).</p><div class="grid">
    <div class="card"><h4>HSS</h4>
      <label class="chk"><input type="checkbox" id="d_hss_on" ${$("hss_on").checked?"checked":""}> вкл</label>
      <label>ТФ</label><select id="d_hss_tf">${$("hss_tf").innerHTML}</select>
      <label>R:R</label><input id="d_hss_rr" type="number" step="0.1" value="${$("hss_rr").value}">
      <label>откат</label><input id="d_hss_pb" type="number" value="${$("hss_pb").value}">
      <label>часы UTC+3</label><input id="d_hss_sess" value="${$("hss_sess").value}">
      <label class="chk"><input type="checkbox" id="d_hss_24" ${$("hss_24").checked?"checked":""}> 24/7</label></div>
    <div class="card"><h4>Лондон S/R</h4>
      <label class="chk"><input type="checkbox" id="d_lsr_on" ${$("lsr_on").checked?"checked":""}> вкл</label>
      <label>ТФ</label><select id="d_lsr_tf">${$("lsr_tf").innerHTML}</select>
      <label>R:R</label><input id="d_lsr_rr" type="number" step="0.1" value="${$("lsr_rr").value}">
      <label>Лондон</label><input id="d_lsr_lon" value="${$("lsr_lon").value}">
      <label>NY</label><input id="d_lsr_ny" value="${$("lsr_ny").value}"></div>
    <div class="card"><h4>Flow 5m</h4>
      <label class="chk"><input type="checkbox" id="d_flow_on" ${$("flow_on").checked?"checked":""}> вкл</label>
      <label>ТФ</label><select id="d_flow_tf">${$("flow_tf").innerHTML}</select>
      <label>R:R</label><input id="d_flow_rr" type="number" step="0.1" value="${$("flow_rr").value}"></div>
    <div class="card"><h4>Playbook</h4>
      <label class="chk"><input type="checkbox" id="d_pb_on" ${$("pb_on").checked?"checked":""}> вкл (вместо Flow+S/R на 5m)</label></div>
    <div class="card"><h4>Пробой</h4>
      <label class="chk"><input type="checkbox" id="d_bo_on" ${$("bo_on").checked?"checked":""}> вкл</label>
      <label>ТФ</label><select id="d_bo_tf">${$("bo_tf").innerHTML}</select>
      <label>R:R</label><input id="d_bo_rr" type="number" step="0.1" value="${$("bo_rr").value}"></div>
    <div class="card"><h4>Squeeze</h4>
      <label class="chk"><input type="checkbox" id="d_sq_on" ${$("sq_on").checked?"checked":""}> вкл</label>
      <label>ТФ</label><select id="d_sq_tf">${$("sq_tf").innerHTML}</select>
      <label>R:R</label><input id="d_sq_rr" type="number" step="0.1" value="${$("sq_rr").value}"></div>
  </div>
  <p style="margin-top:12px"><button class="btn b-dim" onclick="pullStrat();save()">Сохранить</button></p>`,
  news:()=>`<p class="hint">Важные новости могут запретить вход.</p>
    <label class="chk"><input type="checkbox" id="d_news_gate" ${$("news_gate").checked?"checked":""}> фильтр</label>
    <label>минут</label><input id="d_news_buf" type="number" value="${$("news_buf").value}" style="max-width:100px">
    <p><button class="btn b-dim" onclick="pullNews();save()">Сохранить</button></p>
    <div id="newsbox" style="margin-top:12px">загрузка…</div>`,
  pairs:()=>{
    const on=[...document.querySelectorAll("#crypto_list [data-c]:checked")].map(x=>x.dataset.c);
    return `<div class="card"><h4>Крипта</h4>
      <p class="hint">Отметьте, чем торговать. BTC и ENA нельзя. Смена списка — сохранить, Стоп, снова Тест.</p>
      <p><button class="btn b-dim" type="button" onclick="tickCoins(STARTER)">старт (4)</button>
         <button class="btn b-dim" type="button" onclick="tickCoins(UNI)">вся вселенная</button></p>
      <div id="d_crypto">${coinBoxes(on)}</div>
      <label>добавить тикер</label><input id="d_add" placeholder="XRP или SOL/USDT:USDT">
      <p><button class="btn b-dim" type="button" onclick="addCoin()">добавить</button></p>
    </div>
      <div class="card" style="margin-top:8px"><h4>MT5</h4><input id="d_mt5" value="${$("mt5_symbols").value}"></div>
      <p style="margin-top:12px"><button class="btn b-dim" onclick="pullPairs();save()">Сохранить</button></p>`;
  }
};
function pullAccount(){
  $("deposit").value=$("d_deposit").value; $("risk_pct").value=$("d_risk").value;
  $("venue").value=$("d_venue").value; $("leverage_crypto").value=$("d_lc").value;
  $("leverage_mt5").value=$("d_lm").value; $("max_daily_loss_pct").value=$("d_ddl").value;
  $("max_drawdown_pct").value=$("d_dd").value; $("exchange").value=$("d_ex").value;
  if($("d_pos")) $("max_open_positions").value=$("d_pos").value;
  if($("d_int")) $("interval_sec").value=$("d_int").value;
  if($("d_allow")) $("allow_live").checked=$("d_allow").checked;
  if($("d_confirm")) $("confirm").value=$("d_confirm").value;
  const rk=$("riskK"); if(rk) rk.textContent=$("risk_pct").value+"%";
}
function pullStrat(){
  [["hss_on","d_hss_on","checked"],["hss_tf","d_hss_tf"],["hss_rr","d_hss_rr"],["hss_pb","d_hss_pb"],
   ["hss_sess","d_hss_sess"],["hss_24","d_hss_24","checked"],["lsr_on","d_lsr_on","checked"],
   ["lsr_tf","d_lsr_tf"],["lsr_rr","d_lsr_rr"],["lsr_lon","d_lsr_lon"],["lsr_ny","d_lsr_ny"],
   ["bo_on","d_bo_on","checked"],["bo_tf","d_bo_tf"],["bo_rr","d_bo_rr"],
   ["sq_on","d_sq_on","checked"],["sq_tf","d_sq_tf"],["sq_rr","d_sq_rr"],
   ["flow_on","d_flow_on","checked"],["flow_tf","d_flow_tf"],["flow_rr","d_flow_rr"],
   ["pb_on","d_pb_on","checked"],
   ["scan_on","d_scan_on","checked"],["scan_k","d_scan_k"],["scan_min","d_scan_min"]
  ].forEach(([a,b,c])=>{ if(!$(b)) return; if(c) $(a).checked=$(b).checked; else $(a).value=$(b).value; });
}
function pullNews(){ $("news_gate").checked=$("d_news_gate").checked; $("news_buf").value=$("d_news_buf").value; }
function tickCoins(list){
  const want=new Set(list||[]);
  document.querySelectorAll("#d_crypto [data-c]").forEach(x=>{ x.checked=want.has(x.dataset.c); });
}
function addCoin(){
  const raw=($("d_add").value||"").trim().toUpperCase();
  if(!raw) return;
  if(raw.startsWith("BTC")||raw.startsWith("ENA")){ toast("BTC и ENA нельзя"); return; }
  let s=raw;
  if(!s.includes("/")) s=s.replace(/USDT$/,"")+"/USDT:USDT";
  if(!UNI.includes(s)) UNI.push(s);
  const on=[...document.querySelectorAll("#d_crypto [data-c]:checked")].map(x=>x.dataset.c);
  if(!on.includes(s)) on.push(s);
  $("d_crypto").innerHTML=coinBoxes(on);
  $("d_add").value="";
}
function pullPairs(){
  $("mt5_symbols").value=$("d_mt5").value;
  const on=[...document.querySelectorAll("#d_crypto [data-c]:checked")].map(x=>x.dataset.c);
  $("crypto_list").innerHTML=coinBoxes(on);
}
async function save(){
  const r=await (await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(collect())})).json();
  toast(r.ok?"сохранено":(r.error||"ошибка"));
}
async function run(mode){
  try{ await save(); }catch(e){}
  const r=await (await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode})})).json();
  $("runmsg").textContent=r.ok?(mode==="paper"?"тест запущен":"демо запущено"):(r.error||"ошибка");
  jobs();
}
async function runLive(){
  await save();
  const r=await (await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({mode:"live",confirm:$("confirm").value})})).json();
  $("runmsg").textContent=r.ok?"бой запущен":r.error; jobs();
}
async function stop(){
  await fetch("/api/stop",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
  $("runmsg").textContent="остановлено"; jobs();
}
async function jobs(){
  const s=await (await fetch("/api/state")).json();
  const j=(s.jobs||[]).filter(x=>x.running);
  $("jobs").textContent=j.length?"бот работает":"бот выключен";
  if(s.crypto_universe) UNI=s.crypto_universe;
  if(s.crypto_starter) STARTER=s.crypto_starter;
  if(s.config && !editing()){ try{ fill(s.config); }catch(e){} }
  const sc=s.scanner||{};
  const on=sc.enabled!==false;
  const hot=(sc.hot||[]).map(h=>(h.symbol||"").split("/")[0]).filter(Boolean);
  $("scanK").textContent=on?(hot.length?hot.join(" · "):"ждёт сетап"):"выкл";
  const box=$("d_scan_now");
  if(box){
    box.textContent=on
      ?(hot.length?"сейчас боту: "+(sc.hot||[]).map(h=>h.symbol.split("/")[0]+" "+h.strategy+" "+h.score).join(", ")
        :"сканер включён, сетапа нет — новых входов нет")
      :"сканер выключен: бот берёт сигналы со всех пар";
  }
}
async function news(){
  const n=await (await fetch("/api/news")).json();
  const box=$("newsbox"); if(!box) return;
  box.innerHTML=((n.calendar&&n.calendar.sections)||[]).filter(s=>s.role==="gate").map(s=>{
    const ev=(s.events||[]).slice(0,4).map(e=>`<div class="ev"><span class="hi">${e.currency}</span> ${(e.time||"").slice(5,16).replace("T"," ")} ${e.title}</div>`).join("")||"<div class='hint'>нет важных</div>";
    return `<div class="card" style="margin-bottom:8px"><h4>${s.title}</h4>${ev}</div>`;
  }).join("");
}

const MIN={pairs:160,ticket:200,side:280,chart:360};
const DEF={pairs:200,ticket:240,side:400};
let layout={...DEF};
try{ Object.assign(layout, JSON.parse(localStorage.getItem("neft_layout")||"{}")); }catch(e){}
let sideId=null;
function sideOn(){ return $("app").classList.contains("side-on"); }
function clampLayout(){
  layout.pairs=Math.max(MIN.pairs,layout.pairs||DEF.pairs);
  layout.ticket=Math.max(MIN.ticket,layout.ticket||DEF.ticket);
  layout.side=Math.max(MIN.side,layout.side||DEF.side);
  let extra=innerWidth-MIN.chart-layout.pairs-layout.ticket-(sideOn()?layout.side:0);
  if(extra>=0) return;
  extra=-extra;
  [["side",sideOn()],["ticket",true],["pairs",true]].forEach(([k,on])=>{
    if(!on||extra<=0) return;
    const take=Math.min(Math.max(0,layout[k]-MIN[k]),extra);
    layout[k]-=take; extra-=take;
  });
}
function applyLayout(){
  clampLayout();
  $("app").style.setProperty("--pairs",layout.pairs+"px");
  $("app").style.setProperty("--ticket",layout.ticket+"px");
  $("app").style.setProperty("--side",layout.side+"px");
  if(typeof resizeChart==="function") resizeChart();
}
function saveLayout(){ localStorage.setItem("neft_layout",JSON.stringify(layout)); }
function closeSide(){
  sideId=null; $("app").classList.remove("side-on");
  document.querySelectorAll(".nav button").forEach(b=>b.classList.remove("on"));
  applyLayout();
}
function openSheet(id){
  if(sideId===id){ closeSide(); return; }
  sideId=id;
  document.querySelectorAll(".nav button").forEach(b=>b.classList.toggle("on",b.dataset.d===id));
  $("sideTitle").textContent={strat:"Стратегии",account:"Счёт",news:"Новости",pairs:"Пары"}[id];
  $("sheet").innerHTML=sheets[id]();
  if(id==="news") news();
  if(id==="account"){ $("d_venue").value=$("venue").value; $("d_ex").value=$("exchange").value; }
  if(id==="strat"){
    $("d_hss_tf").value=$("hss_tf").value; $("d_lsr_tf").value=$("lsr_tf").value;
    $("d_bo_tf").value=$("bo_tf").value; $("d_sq_tf").value=$("sq_tf").value;
  }
  $("app").classList.add("side-on"); applyLayout();
}
document.querySelectorAll(".nav button").forEach(btn=>btn.onclick=()=>openSheet(btn.dataset.d));
$("sideClose").onclick=closeSide;
function maxWidth(col){
  const others=(col==="pairs"?0:layout.pairs)+(col==="ticket"?0:layout.ticket)+(sideOn()&&col!=="side"?layout.side:0);
  return Math.max(MIN[col], innerWidth-MIN.chart-others);
}
let drag=null;
document.querySelectorAll(".grip").forEach(g=>{
  g.onmousedown=e=>{ e.preventDefault(); drag={col:g.dataset.col,x:e.clientX,w:layout[g.dataset.col]}; g.classList.add("drag"); };
});
window.addEventListener("mousemove",e=>{
  if(!drag) return;
  const dir=drag.col==="pairs"?1:-1;
  layout[drag.col]=Math.round(Math.max(MIN[drag.col],Math.min(maxWidth(drag.col),drag.w+dir*(e.clientX-drag.x))));
  applyLayout();
});
window.addEventListener("mouseup",()=>{ if(!drag) return; document.querySelectorAll(".grip").forEach(g=>g.classList.remove("drag")); drag=null; saveLayout(); });
window.addEventListener("resize",applyLayout);
applyLayout();

let C={};
try{ C=JSON.parse(localStorage.getItem("neft_chart")||"{}")||{}; }catch(e){}
const pref={ tf:C.tf||"1m", type:C.type||"candle", ema:C.ema!==false, vol:C.vol!==false, lines:C.lines!==false, emaN:C.emaN||100, scale:C.scale||"0", grid:C.grid!==false };
function savePref(){ localStorage.setItem("neft_chart",JSON.stringify(pref)); }

if(!window.LightweightCharts) $("chart").textContent="нет библиотеки графика";
let chart=null;
try{
  if(window.LightweightCharts) chart=LightweightCharts.createChart($("chart"),{
    layout:{background:{color:"#111"},textColor:"#ccc"},
    grid:{vertLines:{color:"#222"},horzLines:{color:"#222"}},
    timeScale:{timeVisible:true,secondsVisible:false,borderColor:"#2a2a2a"},
    rightPriceScale:{borderColor:"#2a2a2a"},
    crosshair:{mode:0},
    width:Math.max(320,$("chart").clientWidth||800),
    height:Math.max(240,$("chart").clientHeight||400),
  });
}catch(e){ setText("feed","график не создался"); }
let candle,ema,vol,riskBand;
function makeSeries(){
  if(!chart) return;
  try{ if(candle) chart.removeSeries(candle); }catch(e){}
  try{ if(ema) chart.removeSeries(ema); }catch(e){}
  try{ if(vol) chart.removeSeries(vol); }catch(e){}
  try{ if(riskBand) chart.removeSeries(riskBand); }catch(e){}
  candle=ema=vol=riskBand=null;
  const up="#3dcc8a", dn="#e35d6a";
  if(pref.type==="bar") candle=chart.addBarSeries({upColor:up,downColor:dn});
  else if(pref.type==="line") candle=chart.addLineSeries({color:up,lineWidth:2});
  else if(pref.type==="area") candle=chart.addAreaSeries({lineColor:up,topColor:"#3dcc8a55",bottomColor:"#3dcc8a08"});
  else candle=chart.addCandlestickSeries({upColor:up,downColor:dn,borderVisible:false,wickUpColor:up,wickDownColor:dn});
  ema=chart.addLineSeries({color:"#888",lineWidth:1,priceLineVisible:false,lastValueVisible:false});
  try{
    vol=chart.addHistogramSeries({priceFormat:{type:"volume"},color:"#333"});
    vol.priceScale().applyOptions({scaleMargins:{top:0.84,bottom:0}});
  }catch(e){ vol=null; }
  try{
    riskBand=chart.addBaselineSeries({
    baseValue:{type:"price",price:0},
    topLineColor:"rgba(0,0,0,0)",bottomLineColor:"rgba(0,0,0,0)",
    topFillColor1:"rgba(61,204,138,.14)",topFillColor2:"rgba(61,204,138,.02)",
    bottomFillColor1:"rgba(227,93,106,.14)",bottomFillColor2:"rgba(227,93,106,.02)",
    lineWidth:0,priceLineVisible:false,lastValueVisible:false,
    });
  }catch(e){ riskBand=null; }
}
if(chart){ try{ makeSeries(); }catch(e){ setText("feed","серии графика"); } }
let priceLines=[], active=null, lastKey="", lastOverlay="", liveWs=null, lastClose=null, liveBars=[], bookNow=null;

function resizeChart(){
  if(!chart) return;
  const w=$("chart").clientWidth, h=$("chart").clientHeight;
  if(w>20&&h>20) chart.resize(w,h);
}
function applyChartOpts(){
  if(!chart) return;
  chart.applyOptions({grid:{vertLines:{visible:pref.grid,color:"#222"},horzLines:{visible:pref.grid,color:"#222"}}});
  chart.priceScale("right").applyOptions({mode:+pref.scale});
  if(ema) ema.applyOptions({visible:pref.ema});
  if(vol) vol.applyOptions({visible:pref.vol});
}
function marketOf(b){
  const [sym,tf]=(b.key||"").split("|");
  const pair=b.pair||((sym||"BTC/USDT").split("/")[0]+"USDT");
  return {pair, venue:b.venue||(pair.startsWith("HYPE")?"bybit":"binance"), tf:b.tf||tf||"1m"};
}
function setPrice(v){
  const el=$("lastPx");
  el.textContent=Number(v).toLocaleString("en-US",{maximumSignificantDigits:8});
  if(lastClose!=null) el.className="px "+(v>=lastClose?"up":"down");
  lastClose=v;
}
function emaCalc(bars,p){
  const k=2/(p+1); let e=null,out=[];
  for(const c of bars){ const px=c.close??c.value; e=e==null?px:px*k+e*(1-k); out.push({time:c.time,value:e}); }
  return out;
}
function asMain(bars){
  if(pref.type==="line"||pref.type==="area") return bars.map(c=>({time:c.time,value:c.close??c.value}));
  return bars;
}
function applyBands(s,bars){
  if(!riskBand) return;
  if(!pref.lines||!s||!bars||bars.length<2){ riskBand.setData([]); return; }
  const entry=Number(s.entry||s.price), sl=Number(s.sl), tp=Number(s.tp);
  if(!entry||!sl||!tp){ riskBand.setData([]); return; }
  riskBand.applyOptions({baseValue:{type:"price",price:entry}});
  riskBand.setData([{time:bars[0].time,value:s.side==="sell"?sl:tp},{time:bars[bars.length-1].time,value:s.side==="sell"?sl:tp}]);
}
function levelsHtml(b){
  const s=b.setup;
  if(!s) return `Сейчас бот ждёт сетап на ${b.label}.<br><span class="dim">сделок ${b.trades} · итог ${b.pnl>=0?"+":""}${money(b.pnl)}</span>`;
  const entry=s.entry||s.price;
  const side=s.side==="sell"?"продажа":"покупка";
  const cls=s.side==="sell"?"sell":"buy";
  const kind=s.kind==="position"?"В сделке":s.kind==="pending"?"Ждёт цену входа":"Последний сигнал";
  return `<div class="${cls}">${kind}: ${side}</div>
    <div class="t">стратегия</div><div class="v">${s.strategy||"—"}</div>
    <div class="t">вход</div><div class="v">${n(entry)}</div>
    <div class="t">стоп</div><div class="v sell">${n(s.sl)}</div>
    <div class="t">цель</div><div class="v buy">${n(s.tp)}</div>`;
}
function applyOverlay(b){
  if(!candle) return;
  const sig=JSON.stringify({lines:b.lines,m:b.markers,s:b.setup,L:pref.lines});
  if(sig===lastOverlay) return;
  lastOverlay=sig;
  if(candle.setMarkers) candle.setMarkers(pref.lines?(b.markers||[]):[]);
  priceLines.forEach(l=>{ try{ candle.removePriceLine(l);}catch(e){} });
  priceLines=[];
  if(pref.lines) priceLines=(b.lines||[]).map(l=>candle.createPriceLine({
    price:l.price,color:l.color,lineWidth:l.title==="вход"?2:1,
    lineStyle:l.style===2?2:l.style===3?3:0,axisLabelVisible:true,title:l.title,
  }));
  applyBands(b.setup, liveBars.length?liveBars:b.candles);
  $("levels").innerHTML=levelsHtml(b);
}
function cleanBars(bars){
  const seen=new Set(), out=[];
  for(const x of bars||[]){
    const t=Math.floor(Number(x.time));
    if(!t||seen.has(t)) continue;
    seen.add(t);
    const o=+x.open, h=+x.high, l=+x.low, c=+x.close;
    if(![o,h,l,c].every(Number.isFinite)) continue;
    out.push({time:t,open:o,high:h,low:l,close:c,volume:+x.volume||0});
  }
  return out.sort((a,b)=>a.time-b.time);
}
function paintBars(bars){
  if(!candle) return;
  liveBars=cleanBars(bars);
  try{ candle.setData(asMain(liveBars)); }catch(e){ return; }
  try{ if(ema) ema.setData(pref.ema?emaCalc(liveBars,pref.emaN):[]); }catch(e){}
  try{
    if(vol) vol.setData(liveBars.map(x=>({
      time:x.time,value:x.volume,color:x.close>=x.open?"#2a4":"#622",
    })));
  }catch(e){}
  if(liveBars.length) setPrice(liveBars[liveBars.length-1].close);
}
async function loadMarket(b){
  const m=marketOf(b);
  if(pref.tf===m.tf && b.candles&&b.candles.length){ paintBars(b.candles); return; }
  try{
    const r=await (await fetch(`/api/klines?pair=${m.pair}&tf=${pref.tf}&venue=${m.venue}`)).json();
    if(r.candles&&r.candles.length){ paintBars(r.candles); return; }
  }catch(e){}
  paintBars(b.candles||[]);
}
function connectLive(b){
  if(liveWs){ try{ liveWs.close(); }catch(e){} liveWs=null; }
  const m=marketOf(b);
  $("feed").className="live"; $("feed").textContent="подключение…";
  const bybitIv={"1m":"1","5m":"5","15m":"15","1h":"60","4h":"240","1d":"D"}[pref.tf]||"1";
  let ws;
  if(m.venue==="bybit"){
    ws=new WebSocket("wss://stream.bybit.com/v5/public/linear");
    ws.onopen=()=>ws.send(JSON.stringify({op:"subscribe",args:[`kline.${bybitIv}.${m.pair}`]}));
    ws.onmessage=ev=>{
      const row=((JSON.parse(ev.data).data)||[])[0];
      if(!row||!row.start) return;
      pushLive({time:Math.floor(+row.start/1000),open:+row.open,high:+row.high,low:+row.low,close:+row.close,volume:+row.volume},b);
    };
  } else {
    ws=new WebSocket(`wss://fstream.binance.com/ws/${m.pair.toLowerCase()}@kline_${pref.tf}`);
    ws.onmessage=ev=>{
      const k=JSON.parse(ev.data).k; if(!k) return;
      pushLive({time:Math.floor(k.t/1000),open:+k.o,high:+k.h,low:+k.l,close:+k.c,volume:+k.v},b);
    };
  }
  const prev=ws.onopen;
  ws.onopen=()=>{ if(prev) prev(); $("feed").className="live on"; $("feed").textContent="живой рынок · "+pref.tf; };
  ws.onclose=()=>{ if(liveWs===ws) setTimeout(()=>{ if(lastKey===b.key) connectLive(b); },1500); };
  liveWs=ws;
}
function pushLive(bar,b){
  if(!candle) return;
  const i=liveBars.findIndex(x=>x.time===bar.time);
  if(i>=0) liveBars[i]=bar; else liveBars.push(bar);
  try{ candle.update(asMain([bar])[0]); }catch(e){}
  try{ if(vol) vol.update({time:bar.time,value:bar.volume||0,color:bar.close>=bar.open?"#2a4":"#622"}); }catch(e){}
  if(pref.ema&&ema){ const e=emaCalc(liveBars,pref.emaN); if(e.length) try{ ema.update(e[e.length-1]); }catch(e){} }
  setPrice(bar.close);
  if(pref.lines&&riskBand&&b&&b.setup){
    const t0=liveBars[0]?liveBars[0].time:bar.time;
    try{ riskBand.setData([
      {time:t0,value:b.setup.side==="sell"?b.setup.sl:b.setup.tp},
      {time:bar.time,value:b.setup.side==="sell"?b.setup.sl:b.setup.tp},
    ]); }catch(e){}
  }
}
async function pick(data,key,force){
  if(!data.books||!data.books.length) return;
  active=key||active||data.books[0].key;
  const b=data.books.find(x=>x.key===active)||data.books[0];
  active=b.key; bookNow=b;
  if(lastKey!==b.key||force){
    lastKey=b.key; lastOverlay=""; lastClose=null;
    await loadMarket(b); applyOverlay(b); connectLive(b);
    if(chart) chart.timeScale().fitContent();
    $("symName").textContent=b.label;
  } else applyOverlay(b);
  $("list").innerHTML=data.books.map(x=>
    `<button class="sym ${x.key===active?"on":""}" data-k="${x.key}"><span>${x.label}</span><small>${shortStatus(x.status)}</small></button>`).join("");
  document.querySelectorAll(".sym").forEach(el=>el.onclick=()=>pick(data,el.dataset.k,true));
  $("foot").textContent=(b.strategies||[]).join(" · ");
}
function syncToolbar(){
  document.querySelectorAll("[data-tf]").forEach(b=>b.classList.toggle("on",b.dataset.tf===pref.tf));
  document.querySelectorAll("[data-type]").forEach(b=>b.classList.toggle("on",b.dataset.type===pref.type));
  $("btnEma").classList.toggle("on",pref.ema);
  $("btnVol").classList.toggle("on",pref.vol);
  $("btnLines").classList.toggle("on",pref.lines);
  $("optScale").value=pref.scale; $("optEma").value=pref.emaN; $("optGrid").checked=pref.grid;
}
async function reloadChart(){
  savePref(); applyChartOpts();
  if(!bookNow) return;
  lastOverlay="";
  await loadMarket(bookNow); applyOverlay(bookNow); connectLive(bookNow);
  if(chart) chart.timeScale().fitContent();
}
document.querySelectorAll("[data-tf]").forEach(btn=>btn.onclick=()=>{ pref.tf=btn.dataset.tf; syncToolbar(); reloadChart(); });
document.querySelectorAll("[data-type]").forEach(btn=>btn.onclick=()=>{
  pref.type=btn.dataset.type; savePref(); makeSeries(); applyChartOpts();
  if(liveBars.length) paintBars(liveBars);
  if(bookNow){ lastOverlay=""; applyOverlay(bookNow); }
  syncToolbar();
});
$("btnEma").onclick=()=>{ pref.ema=!pref.ema; syncToolbar(); savePref(); if(ema) ema.setData(pref.ema?emaCalc(liveBars,pref.emaN):[]); };
$("btnVol").onclick=()=>{ pref.vol=!pref.vol; syncToolbar(); savePref(); if(vol) vol.applyOptions({visible:pref.vol}); };
$("btnLines").onclick=()=>{ pref.lines=!pref.lines; syncToolbar(); savePref(); if(bookNow){ lastOverlay=""; applyOverlay(bookNow);} };
$("btnFit").onclick=()=>{ if(chart) chart.timeScale().fitContent(); };
$("btnGear").onclick=e=>{ e.stopPropagation(); $("chartPop").classList.toggle("show"); };
$("optScale").onchange=$("optEma").onchange=$("optGrid").onchange=()=>{
  pref.scale=$("optScale").value; pref.emaN=+$("optEma").value||100; pref.grid=$("optGrid").checked;
  savePref(); applyChartOpts(); if(pref.ema&&ema) ema.setData(emaCalc(liveBars,pref.emaN));
};
document.addEventListener("click",e=>{ if(!e.target.closest(".ctb")) $("chartPop").classList.remove("show"); });
try{ syncToolbar(); applyChartOpts(); }catch(e){}

async function showMarket(pair, tf, venue){
  const coin=pair.replace("USDT","");
  const fake={ key:pair+"|"+tf, label:coin+" "+tf, pair, venue, tf,
    candles:[], lines:[], markers:[], setup:null, trades:0, pnl:0,
    status:"рынок", strategies:[] };
  bookNow=fake; lastKey=""; lastOverlay="";
  $("symName").textContent=fake.label;
  await loadMarket(fake);
  connectLive(fake);
  if(chart) chart.timeScale().fitContent();
  resizeChart();
}
function defaultList(){
  const raw=(S.crypto_symbols&&S.crypto_symbols.length)?S.crypto_symbols
    :(UNI.length?UNI:["BTC/USDT:USDT","ETH/USDT:USDT","SOL/USDT:USDT","BNB/USDT:USDT"]);
  const coins=[...raw];
  if(!coins.some(s=>String(s).startsWith("BTC"))) coins.unshift("BTC/USDT:USDT");
  $("list").innerHTML=coins.map(s=>{
    const c=String(s).split("/")[0];
    return `<button class="sym" data-p="${c}USDT"><span>${c} ${pref.tf}</span><small>рынок</small></button>`;
  }).join("");
  document.querySelectorAll("#list .sym").forEach(el=>el.onclick=()=>showMarket(el.dataset.p, pref.tf, "binance"));
}

async function chartTick(){
  try{
    const data=await (await fetch("/crypto_chart.json?t="+Date.now())).json();
    if(data.equity!=null) $("eq").textContent=money(data.equity);
    const tot=(data.books||[]).reduce((s,x)=>s+(x.pnl||0),0);
    if(data.books) $("pnl").textContent=(tot>=0?"+":"")+money(tot);
    if(data.risk!=null){ const rk=$("riskK"); if(rk) rk.textContent=data.risk+"%"; }
    if(data.books&&data.books.length) await pick(data, active);
  }catch(e){}
}

(async()=>{
  try{ resizeChart(); }catch(e){}
  try{ await jobs(); }catch(e){}
  try{ defaultList(); }catch(e){}
  try{ await showMarket("BTCUSDT", pref.tf||"1m", "binance"); }catch(e){ setText("feed","нет свечей"); }
  try{ chartTick(); }catch(e){}
  setInterval(()=>jobs().catch(()=>{}),4000);
  setInterval(()=>chartTick().catch(()=>{}),2000);
})();
