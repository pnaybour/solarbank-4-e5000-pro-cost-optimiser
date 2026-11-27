"use strict";
/* The Anker-style home page (owner, 19 November 2026): battery ring, what it is doing, household mode, tonight, the money and the day chart.
   Painted from the same /api/status as the rest of the page (renderHome is called by dashboard.js's render), plus /api/day for the chart.
   Nothing here sends anything: the household buttons are dashboard.js's, and the real-time panel is on the Settings page (Real-time diagnostics). */
const HOME={day:null,metric:"battery_kw",data:null,loaded:0,tried:0,failed:false,seq:0,pending:0};   // seq numbers each request; pending is the one in flight (0 = none)
const METRIC={battery_kw:{title:"Battery",up:"out",down:"in",colour:"#2f9e6a",fill:"#cdeedd"},house_kw:{title:"House",up:"use",colour:"#5b7cfa",fill:"#dfe6ff"},
 solar_kw:{title:"Solar",up:"made",colour:"#e0a800",fill:"#fdf0c4"},grid_kw:{title:"Grid",up:"bought",down:"sold",colour:"#7a8a82",fill:"#e7ece9"}};
const watts=kw=>kw===null||kw===undefined ? "—" : Math.abs(kw)<1 ? Math.round(Math.abs(kw)*1000)+" W" : Math.abs(kw).toFixed(2)+" kW";
const hm=t=>new Date(t*1000).toLocaleTimeString("en-GB",{timeZone:"Europe/London",hour:"2-digit",minute:"2-digit"});
const text=(id,value)=>{const node=el(id);if(node)node.textContent=value;};

function homeState(r,live,decision){
 const b=r.battery_power.value;
 if(b===null)return ["No fresh reading",""];
 if(live && decision && decision.discharge==="INHIBIT" && !(decision.charge_limit_w>0))return ["Holding",(decision.restrictions||[]).some(x=>x.startsWith("ev_")) ? "Not discharging while the car charges" : "Full for now: waiting for the cheap rate to end"];
 if(b>0.05)return ["Powering the house",watts(b)+" from the battery"];
 if(b<-0.05)return ["Charging",watts(b)+(state.anker_now && state.anker_now.from_grid ? " from the grid" : " from solar")];
 return ["Idle","Neither charging nor discharging"];
}
function modeLine(live,ctl){
 if(!ctl || !ctl.present)return "Controller not reporting · the Anker app is in charge";
 if(!live)return "Watching only · the Anker app is in charge";
 const d=ctl.decision||{};
 if(d.charge_limit_w>0)return "Charging at the cheap rate · up to "+(d.charge_limit_w/1000).toFixed(1)+" kW"+(d.charge_target_soc!=null ? " to "+Math.round(d.charge_target_soc*100)+"%" : "");
 if(d.discharge==="INHIBIT")return "Holding the battery at 0 W · third-party control";
 return "Self-consumption · handed back to the Anker";
}
function renderTonightHome(live){
 const p=state.shadow_plan||{},w=p.next_window,target=p.grid_target!=null ? p.grid_target : (w ? w.target : null);
 if(target==null || !w){text("home-tonight-main","No plan yet");text("home-tonight-detail",p.reason ? p.reason+"." : "The planner has not produced a checked plan yet.");return;}
 const tariff=state.tariff,startMinute=Number(hm(w.start).slice(0,2))*60+Number(hm(w.start).slice(3));
 const slot=tariff && startMinute!==tariff.start_minute;
 const today=new Date().toLocaleDateString("en-CA",{timeZone:"Europe/London"}),day=new Date(w.start*1000).toLocaleDateString("en-CA",{timeZone:"Europe/London"});
 text("home-tonight-label",slot ? "Next cheap slot" : day===today || Date.now()/1000>=w.start ? "Tonight" : "Next cheap-rate charge");
 text("home-tonight-main","Charge to "+Math.round(target*100)+"% from "+hm(w.start));
 const parts=["Cheap rate "+hm(w.start)+" to "+hm(w.end)];
 if(slot)parts.push("a smart slot: the battery charges only while the car is charging");
 if(p.expected_cost_gbp!=null)parts.push("about "+pounds(p.expected_cost_gbp)+" to buy over the next 48 hours");
 if(!live)parts.push("advice only");
 text("home-tonight-detail",parts.join(" · "));
}
function renderMoney(){
 const v=state.savings;
 if(!v){text("home-saving","—");return;}
 const l=v.last_day;
 text("home-saving-label",l && l.date ? "Saving on "+new Date(l.date+"T12:00:00Z").toLocaleDateString("en-GB",{weekday:"short",day:"numeric",month:"short"}) : "Yesterday's saving");
 text("home-saving",l && l.saving_gbp!=null ? (l.saving_gbp>=0 ? "+" : "−")+pounds(Math.abs(l.saving_gbp)) : "—");
 text("home-week",v.last_7_days && v.last_7_days.days_included ? pounds(v.last_7_days.saving_gbp) : "—");
 text("home-live",v.cumulative_counted ? pounds(v.cumulative_counted.saving_gbp) : "—");
}
function renderHome(){
 if(!el("home-hero") || !state)return;
 const r=state.readings,live=isLive(state),ctl=state.realtime && state.realtime.controller;
 const soc=r.soc.value;
 text("home-soc",soc===null ? "—" : Math.round(soc*100)+"%");
 el("home-ring").setAttribute("stroke-dasharray",(soc===null ? 0 : Math.max(0,Math.min(100,soc*100)))+" 100");
 const [headline,detail]=homeState(r,live,ctl && ctl.decision);
 text("home-state",headline);text("home-power",detail);
 text("home-min","Minimum level "+Math.round(state.min_soc*100)+"%");
 text("home-solar",r.pv_power.value===null ? "—" : watts(Math.max(0,r.pv_power.value)));
 const g=r.grid_power.value;
 text("home-grid-label",g!==null && g<-0.05 ? "Grid · selling" : "Grid");
 text("home-grid",g===null ? "—" : watts(g));
 const car={charging:"Charging",standby:"Idle",confirming_start:"Starting",confirming_stop:"Stopping"}[state.charger_state]||"Unknown";
 text("home-car",car==="Charging" && r.ev_power.value!==null ? watts(r.ev_power.value) : car);
 text("home-mode",modeLine(live,ctl));
 renderTonightHome(live);
 renderMoney();
 if(!HOME.pending && HOME.day===null && Date.now()-HOME.tried>(HOME.failed ? 30000 : 300000))loadDay(null,false);      // today's chart refreshes every five minutes (a failed try is repeated after 30 s), never while another request is in flight
}

/* ---- the day chart ------------------------------------------------------------------------------------------------------------------------------------- */
/* Reliability (owner, 20 November 2026: the day arrows "do not work reliably"). One request counts at a time: each load is numbered and an answer that is no
   longer the latest is dropped, so a slow reply (or today's own five-minute refresh) can never overwrite the day just chosen. The arrows are held while a day
   they asked for is loading, so two quick taps cannot both aim at the same neighbour. A failed load says which day it could not load and leaves the arrows
   free to try again; it never leaves a chart that looks as if it were the answer. */
function setArrows(){
 const d=HOME.data;
 el("home-prev").disabled=!!HOME.byUser || !d || !d.previous;
 el("home-next").disabled=!!HOME.byUser || !d || !d.next;
}
async function loadDay(day,byUser){
 const mine=++HOME.seq,wasFailed=HOME.failed;
 HOME.pending=mine;HOME.tried=Date.now();HOME.byUser=!!byUser;
 if(byUser){setArrows();text("home-chart-read","Loading…");}
 try{
  const response=await fetch("/api/day"+(day ? "?day="+encodeURIComponent(day) : ""),{signal:AbortSignal.timeout(15000)});
  if(!response.ok)throw new Error("day");
  const data=await response.json();
  if(mine!==HOME.seq)return;                     // a newer request has been made: this answer is out of date
  HOME.data=data;HOME.day=day;HOME.loaded=Date.now();HOME.failed=false;
  drawDay();
  if(byUser || wasFailed)text("home-chart-read","Tap the chart to read a value.");   // clears "Loading…" or an earlier failure, but not a value the person is reading
 }catch(error){
  if(mine!==HOME.seq)return;
  HOME.failed=true;
  // a quiet background refresh keeps the picture it has; a day the person asked for, or a chart that never came at all, says so plainly
  if(!HOME.data)text("home-chart-read","The day's readings could not be loaded. The page tries again in 30 seconds.");
  else if(byUser)text("home-chart-read","That day could not be loaded. The chart still shows "+(HOME.data ? el("home-date").textContent : "no day")+". Tap the arrow to try again.");
 }finally{
  if(mine===HOME.seq){HOME.pending=0;HOME.byUser=false;setArrows();}
 }
}
function svgNode(name,attrs,content){const n=document.createElementNS("http://www.w3.org/2000/svg",name);for(const k in attrs)n.setAttribute(k,attrs[k]);if(content)n.textContent=content;return n;}
function drawDay(){
 const d=HOME.data;if(!d)return;
 const m=METRIC[HOME.metric],values=d.series[HOME.metric],t=d.totals;
 text("home-day-title",m.title);
 text("home-day-total",{battery_kw:t.battery_out_kwh.toFixed(1)+" kWh out · "+t.battery_in_kwh.toFixed(1)+" kWh in",house_kw:t.house_kwh.toFixed(1)+" kWh used",
  solar_kw:t.solar_kwh.toFixed(1)+" kWh made"+(d.solar_forecast && d.solar_forecast.length ? " · forecast "+forecastKwh(d.solar_forecast,dataEnd(d)).toFixed(1)+(dataEnd(d)<d.end-600 ? " by "+hm(dataEnd(d)) : "") : ""),grid_kw:t.grid_import_kwh.toFixed(1)+" kWh bought"+(t.grid_export_kwh>0.05 ? " · "+t.grid_export_kwh.toFixed(1)+" kWh sold" : "")}[HOME.metric]);
 if(!d.next)text("home-used",t.battery_out_kwh.toFixed(1)+" kWh");      // the money card's "Battery used today": only from today's data, not the day being viewed
 const label=new Date(d.start*1000+43200000).toLocaleDateString("en-GB",{timeZone:"Europe/London",weekday:"short",day:"numeric",month:"short",year:"numeric"});
 text("home-date",d.next ? label : "Today · "+label);
 setArrows();
 const W=340,top=14,bottom=160,left=4,right=300,span=d.end-d.start,x=s=>left+(s-d.start)/span*(right-left);
 const extra=HOME.metric==="solar_kw" ? [...(d.solar_forecast||[]),...(d.solar_corrected||[])].map(r=>r.kw) : [];
 const known=values.filter(v=>v!==null),hi=Math.max(0.5,...known.map(v=>Math.abs(v)),...extra),signed=HOME.metric==="battery_kw" || HOME.metric==="grid_kw";
 const zero=signed ? (top+bottom)/2 : bottom,scale=signed ? (bottom-top)/2/hi : (bottom-top)/hi,y=v=>zero-v*scale;
 const svg=el("home-chart");svg.replaceChildren();
 for(const [a,b] of d.cheap)svg.append(svgNode("rect",{x:x(a),y:top,width:x(b)-x(a),height:bottom-top,class:"cheap-band"}));
 svg.append(svgNode("line",{x1:left,x2:right,y1:zero,y2:zero,class:"axis"}));
 let path="",open=false;
 values.forEach((v,i)=>{const s=d.start+i*d.step_s+d.step_s/2;if(v===null){if(open){path+="L"+x(s-d.step_s)+" "+zero+" ";open=false;}return;}
  path+=(open ? "L" : "M"+x(s)+" "+zero+" L")+x(s)+" "+y(v)+" ";open=true;});
 if(open)path+="L"+x(d.start+values.length*d.step_s)+" "+zero;
 svg.append(svgNode("path",{d:path,fill:m.fill,stroke:m.colour,"stroke-width":1.5,"stroke-linejoin":"round"}));
 const legend=[];
 if(HOME.metric==="solar_kw"){
  const line=(rows,cls)=>{if(!rows || !rows.length)return false;let p="";rows.forEach((r,i)=>{p+=(i ? "L" : "M")+x(r.start)+" "+y(r.kw)+" L"+x(r.end)+" "+y(r.kw)+" ";});svg.append(svgNode("path",{d:p,class:cls}));return true;};
  if(line(d.solar_forecast,"forecast-line"))legend.push("dashed: forecast made the evening before");
  if(line(d.solar_corrected,"corrected-line"))legend.push("dotted: with the learned correction (not used yet)");
 }
 text("home-chart-key",legend.length ? legend.join(" · ") : "");
 const unit=v=>v>=1 ? v.toFixed(1)+" kW" : Math.round(v*1000)+" W";
 svg.append(svgNode("text",{x:W-2,y:top+4,"text-anchor":"end"},unit(hi)));
 svg.append(svgNode("text",{x:W-2,y:zero+4,"text-anchor":"end"},"0"));
 if(signed)svg.append(svgNode("text",{x:W-2,y:bottom,"text-anchor":"end"},"−"+unit(hi)));
 if(m.up)svg.append(svgNode("text",{x:right-4,y:top+12,"text-anchor":"end",class:"hint"},m.up));
 if(signed && m.down)svg.append(svgNode("text",{x:right-4,y:bottom-4,"text-anchor":"end",class:"hint"},m.down));
 for(const h of [0,6,12,18,24])svg.append(svgNode("text",{x:x(d.start+h*3600),y:bottom+22,"text-anchor":h===0 ? "start" : h===24 ? "end" : "middle"},String(h).padStart(2,"0")+":00"));
 const cursor=svgNode("line",{x1:-10,x2:-10,y1:top,y2:bottom,class:"cursor"});svg.append(cursor);
 svg.onpointerdown=svg.onpointermove=event=>{
  const box=svg.getBoundingClientRect(),px=(event.clientX-box.left)/box.width*W;
  const i=Math.max(0,Math.min(values.length-1,Math.floor((px-left)/(right-left)*values.length))),v=values[i],s=d.start+i*d.step_s;
  cursor.setAttribute("x1",x(s+d.step_s/2));cursor.setAttribute("x2",x(s+d.step_s/2));
  text("home-chart-read",hm(s)+": "+(v===null ? "no reading" : HOME.metric==="battery_kw" ? (v>0.02 ? watts(v)+" out" : v<-0.02 ? watts(v)+" in" : "idle") : HOME.metric==="grid_kw" ? (v<-0.02 ? watts(v)+" sold" : watts(v)+" bought") : watts(Math.max(0,v))));
 };
 const socs=d.series.soc,svg2=el("home-soc-chart");svg2.replaceChildren();
 const sy=v=>96-v*84;
 for(const p of [0,0.5,1]){svg2.append(svgNode("line",{x1:left,x2:right,y1:sy(p),y2:sy(p),class:"grid-line"}));svg2.append(svgNode("text",{x:W-2,y:sy(p)+4,"text-anchor":"end"},Math.round(p*100)+"%"));}
 let line="",on=false;
 socs.forEach((v,i)=>{const s=d.start+i*d.step_s+d.step_s/2;if(v===null){on=false;return;}line+=(on ? "L" : "M")+x(s)+" "+sy(v)+" ";on=true;});
 svg2.append(svgNode("path",{d:line,fill:"none",stroke:"#2f9e6a","stroke-width":2,"stroke-linejoin":"round"}));
 const planKey=[];
 const planLine=(plan,cls)=>{if(!plan || !plan.points || plan.points.length<2)return false;svg2.append(svgNode("path",{d:plan.points.map((q,i)=>(i ? "L" : "M")+x(q.t)+" "+sy(q.soc)).join(" "),class:cls}));return true;};
 if(planLine(d.soc_planned,"plan-line"))planKey.push("dashed: the plan made at "+hm(d.soc_planned.issued_at)+(new Date(d.soc_planned.issued_at*1000).toLocaleDateString("en-CA",{timeZone:"Europe/London"})!==d.date ? " the day before" : ""));
 if(planLine(d.soc_plan_latest,"plan-latest"))planKey.push("dotted: the newest plan ("+hm(d.soc_plan_latest.issued_at)+")");
 text("home-soc-key",planKey.length ? planKey.join(" · ") : "No saved plan for this day (plans are kept 36 hours).");
}
/* A day not over yet (today, or a backup taken part-way) is compared with the forecast only up to its latest reading: like with like. */
function dataEnd(d){const v=d.series.solar_kw;let i=v.length-1;while(i>=0 && v[i]===null)i--;return i<0 ? d.start : d.start+(i+1)*d.step_s;}
function forecastKwh(rows,until){return rows.reduce((sum,r)=>sum+r.kw*Math.max(0,Math.min(r.end,until)-r.start)/3600,0);}
function setMetric(name){
 HOME.metric=name;
 for(const b of document.querySelectorAll(".metric"))b.setAttribute("aria-pressed",String(b.dataset.metric===name));
 drawDay();
}
if(typeof document!=="undefined" && document.addEventListener)document.addEventListener("DOMContentLoaded",()=>{
 for(const b of document.querySelectorAll(".metric"))b.addEventListener("click",()=>setMetric(b.dataset.metric));
 el("home-prev").addEventListener("click",()=>{if(!HOME.byUser && HOME.data && HOME.data.previous)loadDay(HOME.data.previous,true);});
 el("home-next").addEventListener("click",()=>{if(!HOME.byUser && HOME.data && HOME.data.next){const n=HOME.data.next,today=new Date().toLocaleDateString("en-CA",{timeZone:"Europe/London"});loadDay(n===today ? null : n,true);}});
 loadDay(null,false);
 // The pages behind the Settings menu (owner, 19 November 2026: "into Settings"): pause or override, hand control to the Anker app, household plans and
 // the 48-hour forecast. They stay on this page, where this page's script fills them, and are shown one at a time like a Settings screen. Opened from
 // the Settings menu, "back" returns to Settings; opened from this page (for example "Plan a visit"), it returns to where you were.
 let inPage=false;
 for(const page of document.querySelectorAll(".legacy-pages>section")){
  const back=document.createElement("a");back.href="/settings";back.className="back";back.textContent="‹ Settings";
  back.addEventListener("click",event=>{if(inPage){event.preventDefault();history.back();}});
  page.prepend(back);
 }
 const showPage=()=>{
  const target=location.hash && document.getElementById(location.hash.slice(1));
  const page=target && target.closest(".legacy-pages>section");
  for(const p of document.querySelectorAll(".legacy-pages>section"))p.classList.toggle("shown",p===page);
  for(const a of document.querySelectorAll(".legacy-pages a.back"))a.textContent=inPage ? "‹ Back" : "‹ Settings";
  document.body.classList.toggle("legacy-open",!!page);
  for(const a of document.querySelectorAll("header nav a")){const here=page ? a.getAttribute("href")==="/settings" : a.getAttribute("href")==="/";if(here)a.setAttribute("aria-current","page");else a.removeAttribute("aria-current");}
  if(page){if(target.tagName==="DETAILS")target.open=true;if(target===page)window.scrollTo(0,0);else target.scrollIntoView();}
 };
 window.addEventListener("hashchange",()=>{inPage=true;showPage();});showPage();
});
