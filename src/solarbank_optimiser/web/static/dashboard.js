"use strict";
const pounds=value=>"£"+Number(value).toFixed(2);
/* The home page's household buttons and the screens behind the Settings menu (household plans, pause or override, control, the next 48 hours).
   The cards at the top of the home page are home.js's; the real-time panel is diagnostics.js's, on the Settings page. */
let controlLive=false;
function isLive(s){const c=s && s.realtime && s.realtime.controller;return !!(c && c.present && c.mode==="live");}
function renderControlNotice(live){
 controlLive=live;
 if(typeof document!=="undefined" && document.body && document.body.classList)document.body.classList.toggle("control-live",live);
}
const MODE_WORDS={"Away from home":"Away","Normal consumption":"Normal","High demand":"High"};
let pendingMode=null;     // the button just pressed: shown as selected at once, until the server has confirmed (or refused) it
let noteTimer=null;
function householdNote(text,error=false){          // the answer to a household button, in the card itself: the page's message line is off-screen by then
 const note=el("household-note");if(!note)return;
 clearTimeout(noteTimer);note.textContent=text;note.classList.toggle("error",error);
 if(text && !error)noteTimer=setTimeout(()=>{note.textContent="";},8000);
}
function renderHousehold(){
 const shown=pendingMode || state.active_demand;
 for(const button of document.querySelectorAll(".mode-btn")){
  const on=button.dataset.mode===shown;
  button.setAttribute("aria-pressed",on ? "true" : "false");
  button.classList.toggle("selected",on);
 }
 const away=el("away");
 if(document.activeElement!==away && !away.dataset.dirty)away.value=state.demand && state.demand.away_watts!==null && state.demand.away_watts!==undefined ? state.demand.away_watts : "";
}
async function chooseMode(mode){
 householdNote("");
 const typed=el("away").value.trim();
 const remembered=state.demand && state.demand.away_watts!==undefined ? state.demand.away_watts : null;
 const away=typed==="" ? remembered : Number(typed);
 if(mode==="Away from home" && (away===null || !Number.isFinite(away) || away<0 || away>25000)){householdNote("Enter the Away base load below first: the power the house uses while nobody is home, in watts.",true);el("away").focus();return;}
 if(state.active_demand===mode && (mode!=="Away from home" || away===remembered)){householdNote(MODE_WORDS[mode]+" is already set.");return;}
 pendingMode=mode;                       // latch the button immediately: the save and the page refresh take a second or two
 renderHousehold();
 el("household-schedule").textContent="Saving…";
 try{
  await save("/api/demand-schedule",{revision:state.demand_revision,mode,start_local:null,end_local:null,start_fold:null,end_fold:null,away_watts:away,high_extra_kwh:null});
 }catch(error){
  householdNote(error.message,true);return;
 }finally{
  pendingMode=null;                     // confirmed (the refresh has the new state) or refused (the buttons go back to what the server says)
  el("household-schedule").textContent="";
  renderHousehold();
 }
 el("away").dataset.dirty="";
 householdNote(MODE_WORDS[mode]+" from now. The charging plan updates shortly.");
}
function renderAutomation(){
 const a=state.automation||{},until=t=>new Date(t*1000).toLocaleString("en-GB",{timeZone:"Europe/London",weekday:"short",day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"});
 const parts=[];
 if(a.paused)parts.push("Paused "+(a.paused_until ? "until "+until(a.paused_until) : "until you return to automatic")+": the optimiser asks for no charging and the Anker runs as normal.");
 if(a.override)parts.push("Override: charge to "+Math.round(a.override.target*100)+"% until "+until(a.override.valid_until)+".");
 el("automation-state").textContent=parts.length ? parts.join(" ") : "Automatic: the planner chooses the charge level.";
 el("resume").disabled=!(a.paused || a.override);
}
function render(){
 renderControlNotice(isLive(state));
 if(typeof renderHome==="function")renderHome();          // the Anker-style top of the home page (home.js), painted from the same status
 renderAutomation();
 renderHousehold();
 el("control-reason").textContent=state.mode_reason || (controlLive ? "Live control: see Real-time diagnostics in Settings." : "No equipment commands. Control has not been commissioned.");
 el("mode").textContent=state.mode==="observe" ? "Observe only" : controlLive ? "Live control" : state.mode.replaceAll("_"," ");     // "shadow" beside a live controller misled (owner, 19 November)
 el("active-demand").textContent=state.active_demand;
 const check=state.forecast && state.forecast.normal_sense_check;
 el("normal-reference").textContent=check && check.mean_kwh!==undefined ? "Normal-use reference: "+check.mean_kwh.toFixed(2)+" ± "+check.sample_sd_kwh.toFixed(2)+" kWh/day (sample SD, "+check.sample_days+" days). "+(check.days.length ? check.days.map(day=>day.day+": forecast "+day.forecast_kwh.toFixed(2)+" kWh"+(day.check_needed ? " · review against reference" : " · within reference range")).join("; ") : "Waiting for a complete Normal-use forecast to compare.") : "";
 const d=state.demand;
 el("household-schedule").textContent=d ? (d.start>Date.now()/1000 ? "Planned: "+d.mode+" from "+londonTime(d.start) : "Current household setting")+(d.end ? ". Returns to Normal "+londonTime(d.end) : "") : "Default household setting. Tap Away or High when it changes.";
 el("slot-status").textContent=Array.isArray(state.extra_slots) ? "Verified extra cheap slots: "+state.extra_slots.map(s=>londonTime(s.start)+" to "+londonTime(s.end)).join("; ") : "No verified extra cheap slots.";
 const f=state.forecast;
 const fresh=f && f.fresh && f.settings_current;
 el("forecast-demand").textContent=fresh && f.demand_kwh!==null ? f.demand_kwh.toFixed(1)+" kWh" : "Unavailable";
 el("forecast-solar").textContent=fresh && f.pv_kwh!==null ? f.pv_kwh.toFixed(1)+" kWh" : "Unavailable";
 el("forecast-reason").textContent=!fresh ? "A current forecast is not available yet." : f.demand_reason || "Weather and household estimates; actual use may differ.";
}
function disconnected(){
 if(typeof text==="function"){text("home-state","Connection lost");text("home-power","");text("home-tonight-main","Connection lost");text("home-tonight-detail","The page reconnects by itself.");}     // the cards the person actually sees
 el("forecast-demand").textContent="Unavailable";el("forecast-solar").textContent="Unavailable";
 el("forecast-reason").textContent="Dashboard connection unavailable.";
}
for(const button of document.querySelectorAll(".mode-btn"))button.addEventListener("click",action(()=>chooseMode(button.dataset.mode)));
el("away").addEventListener("input",()=>{el("away").dataset.dirty="1";});
startPolling();
for(const [id,mode] of [["observe","observe"],["shadow","shadow"],["handover","handover_pending"]])el(id).addEventListener("click",action(async()=>{await save("/api/mode",{revision:state.mode_revision,mode});message(mode==="handover_pending" ? "Handover request recorded. Release to Anker has not been verified; check operation in the Anker app." : (controlLive ? "Mode saved." : "Mode saved. Equipment writes remain disabled."));}));
el("pause").addEventListener("click",action(async()=>{const until=el("pause-until").value;await save("/api/automation",{action:"pause",until_local:until||null,until_fold:null,target_percent:null});message(until ? "Automation paused until "+until.replace("T"," ")+" London time." : "Automation paused until you return to automatic.");}));
el("resume").addEventListener("click",action(async()=>{await save("/api/automation",{action:"resume",until_local:null,until_fold:null,target_percent:null});message("Back to automatic: the planner chooses the charge level.");}));
el("override-set").addEventListener("click",action(async()=>{
 const percent=Number(el("override-target").value),until=el("override-until").value;
 if(!el("override-target").value.trim() || !Number.isFinite(percent))throw new Error("Enter the charge level to hold, as a percentage.");
 if(!until)throw new Error("Choose the time the override should end.");
 await save("/api/automation",{action:"override",until_local:until,until_fold:null,target_percent:percent});
 message("Override set: charge to "+Math.round(percent)+"% until "+until.replace("T"," ")+" London time. The plan will update shortly.");}));
el("plan-save").addEventListener("click",action(async()=>{
 const mode=el("plan-mode").value,from=el("plan-from").value,until=el("plan-until").value;
 if(!until)throw new Error("Choose the end time (when the visit finishes or you return).");
 if(from && from>=until)throw new Error("The end must be after the start.");
 const typed=el("away").value.trim(),remembered=state.demand && state.demand.away_watts!==undefined ? state.demand.away_watts : null;
 const away=mode==="Away from home" ? (typed==="" ? remembered : Number(typed)) : null;
 if(mode==="Away from home" && (away===null || !Number.isFinite(away) || away<0 || away>25000))throw new Error("Enter the Away base load first, in watts.");
 await save("/api/demand-schedule",{revision:state.demand_revision,mode,start_local:from||null,end_local:until,start_fold:null,end_fold:null,away_watts:away,high_extra_kwh:null});
 message("Planned: "+MODE_WORDS[mode]+(from ? " from "+from.replace("T"," ") : " from now")+" until "+until.replace("T"," ")+" (London time). The charging plan will update shortly.");}));
