"use strict";
function render(loadSettings){
 if(typeof renderRealtime==="function")renderRealtime(state.realtime);      // diagnostics.js: the real-time panel on this page
  el("shadow-diagnostics").textContent=state.shadow_plan.reason+(state.shadow_plan.last_attempt ? " · Last run: "+londonTime(state.shadow_plan.last_attempt.time) : "");
  const sp=state.shadow_plan&&state.shadow_plan.series;
  if(sp&&sp.length&&el("plan-series")){
   const table=document.createElement("table"), head=document.createElement("tr");
   for(const h of ["Hour","Solar kWh","Battery kWh (+charge)","Home kWh","From grid kWh","Battery %","Price £/kWh"]){const c=document.createElement("th");c.textContent=h;head.append(c);}
   table.append(head);
   for(const r of sp.slice(0,24)){const row=document.createElement("tr");for(const v of [r.london.slice(5),r.solar_kwh.toFixed(2),r.battery_kwh.toFixed(2),r.home_kwh.toFixed(2),r.grid_import_kwh.toFixed(2),Math.round(r.soc_end*100),r.price_gbp_per_kwh.toFixed(3)]){const c=document.createElement("td");c.textContent=v;row.append(c);}table.append(row);}
   el("plan-series").replaceChildren(table);
  }
  const dl=state.demand_learning;
  if(dl&&el("learning-text")){
   const a=dl.away, n=dl.normal;
   el("learning-text").textContent=(dl.enabled ? "On. " : "Off (typed figures in use). ")+
    "Away: typed "+(a.typed_w!==null ? a.typed_w+" W" : "none")+", learned target "+(a.learned_target_w!==null ? a.learned_target_w+" W" : "not yet")+", in use "+(a.in_use_w!==null ? a.in_use_w+" W" : "n/a")+" ("+a.days+" away days"+(a.promoted ? ", learned figure is winning" : "")+"). "+
    "Normal: "+n.days+" days seen, "+(n.promoted ? "averaging "+n.average_days+" days" : "using the last good day")+".";
   el("learning-toggle").textContent=dl.enabled ? "Switch off" : "Switch on";
   el("learning-toggle").onclick=action(()=>save("/api/demand-learning",{enabled:!dl.enabled}));
  }
  el("mode").textContent=state.mode.replaceAll("_"," ");
  el("tariff-status").textContent=state.tariff_status;
  const nw=state.night_window;
  el("night-window").textContent=nw ? nw.text : "not known (save the tariff below)";
  el("night-window-note").textContent=nw ? [nw.saved && nw.saved.text!==nw.text.split(",")[0] ? "Saved on this page: "+nw.saved.text+"." : "", nw.note||""].filter(Boolean).join(" ") : "";
  if(state.calibration){
   if(document.activeElement!==el("calibration-days"))el("calibration-days").value=state.calibration.max_days;
   const c=state.calibration;
   el("calibration-note").textContent=(c.days_since!==null ? "Last full charge "+c.days_since.toFixed(1)+" days ago. " : "No full charge seen yet. ")+(c.state==="due" ? "A full charge is due, so the next cheap window charges to 100%." : c.state==="unknown" ? "Not enough history yet to say whether one is due." : "A full charge becomes due after "+c.due_after_days+" days.");
  }
  if(document.activeElement!==el("min-soc"))el("min-soc").value=Math.round(state.min_soc*100);
  if(document.activeElement!==el("fresh-charge"))el("fresh-charge").value=state.freshness.charge_s;
  if(document.activeElement!==el("fresh-idle"))el("fresh-idle").value=state.freshness.idle_s;
  el("freshness-note").textContent="Now "+state.freshness.charge_s+" s for the garage current while charging, "+state.freshness.idle_s+" s otherwise (allowed "+state.freshness_ranges.charge_s.join(" to ")+" s and "+state.freshness_ranges.idle_s.join(" to ")+" s).";
  const floor=state.min_soc_device;
  el("min-soc-device").textContent=floor ? "The Solarbank itself holds "+floor.percent+"% (set in the Anker app)."+(floor.mismatch ? " This differs from the "+floor.settings_percent+"% above: the battery's own limit wins, so change it in the Anker app if you want them to agree." : " It matches.") : "";
  el("min-soc-note").textContent="Now "+Math.round(state.min_soc*100)+"% (allowed "+Math.round(state.min_soc_range[0]*100)+"% to "+Math.round(state.min_soc_range[1]*100)+"%).";
  el("slot-status").textContent=Array.isArray(state.extra_slots) ? "Verified household cheap slots: "+state.extra_slots.map(s=>londonTime(s.start)+" to "+londonTime(s.end)).join("; ") : state.extra_slots;
  el("rate").textContent=state.price_gbp_per_kwh===null ? "— p/kWh" : (state.price_gbp_per_kwh*100).toFixed(2)+" p/kWh";
  if(loadSettings){
    formRevisions={demand:state.demand_revision,tariff:state.tariff_revision,providers:state.providers_revision};
    const p=state.providers;
    for(const [id,key] of [["weather-enabled","weather_enabled"],["prices-enabled","prices_enabled"],["account-enabled","account_enabled"],["eligibility","household_rate_verified"]])el(id).checked=p[key];
    for(const [id,key] of [["product","product"],["tariff-code","tariff"],["account","account"],["device","device"],["eligibility-source","eligibility_source"],["billing-rule","billing_rule"]])el(id).value=p[key];
    el("eligibility-until").value=p.eligibility_until===null ? "" : new Date(p.eligibility_until*1000).toISOString();
    if(state.tariff){el("tariff-confirmed").checked=state.tariff.confirmed;el("day").value=state.tariff.day_pence;el("cheap").value=state.tariff.cheap_pence;el("start").value=clock(state.tariff.start_minute);el("end").value=clock(state.tariff.end_minute);el("tariff-until").value=state.tariff.valid_until===null ? "" : new Date(state.tariff.valid_until*1000).toISOString();}
  }
  el("key-status").textContent=state.api_key_configured ? "A private API key is configured. It is not displayed." : "No valid private API key configured.";
  const connection=state.octopus_connection || {};
  el("octopus-connection-status").textContent=connection.reason || "Save the API key, then connect Octopus. No equipment commands are sent.";
  const choices=el("octopus-devices");choices.replaceChildren();
  for(const device of connection.devices || []){const option=document.createElement("option");option.value=device.id;option.label=device.deviceType.replaceAll("_"," ");choices.append(option);}
  const diagnostics=el("provider-diagnostics");diagnostics.replaceChildren();
  for(const [name,d] of Object.entries(state.provider_diagnostics)){const line=document.createElement("p");line.textContent=name+": "+(!d.enabled ? "off" : d.status || "waiting for worker")+(d.reason ? " · "+d.reason.replaceAll("_"," ") : "")+" · Last success: "+(d.last_success ? londonTime(d.last_success) : "none")+" · Cache: "+(d.cache_valid ? "valid" : "unavailable")+(d.next_retry ? " · Next request no earlier than "+londonTime(d.next_retry) : "");diagnostics.append(line);}
  el("tariff-provenance").textContent=state.tariff ? state.tariff.source+(state.tariff.confirmed ? " · Confirmed by you" : " · Check against your current account") : "Enter your account prices; these are used when live rates are unavailable.";
}
function disconnected(){if(typeof renderRealtime==="function")renderRealtime(null);message("Connection unavailable. Settings have not been refreshed.",true);}
async function showDevices(){
 try{
  const response=await fetch("/api/devices",{signal:AbortSignal.timeout(5000)});
  if(!response.ok)return;
  const devices=await response.json(), rows=el("device-rows");
  if(rows.contains(document.activeElement))return;
  rows.textContent="";
  for(const name of ["solarbank","meter","shelly"]){
   const d=devices[name], row=document.createElement("form"), label=document.createElement("label"), input=document.createElement("input"), button=document.createElement("button"), note=document.createElement("span");
   label.textContent=name+" ";input.value=d.in_use||d.configured||"";input.setAttribute("aria-label",name+" address");button.type="submit";button.textContent="Ask to use";
   note.className="fine";
   note.textContent=" "+(d.moved ? "Moved from "+d.configured+" (found again automatically). " : "")+(d.identity_known ? (d.identity_confirmed ? "Recognised and confirmed by you. " : "Recognised ("+(d.identity && d.identity.product ? d.identity.product : "device")+"), not yet confirmed by you: run sudo solarbank devices confirm "+name+". ") : "Not yet recognised. ")+(d.request ? "Request for "+d.request.host+": "+d.request.status+(d.request.reason ? " ("+d.request.reason+")" : "")+"." : "");
   row.append(label,input,button,note);
   row.addEventListener("submit",action(async()=>{await save("/api/devices",{device:name,host:input.value});}));
   rows.append(row);
  }
 }catch(error){}
}
setInterval(showDevices,5000);showDevices();
el("freshness-form").addEventListener("submit",action(async()=>{
 const charge=Number(el("fresh-charge").value),idle=Number(el("fresh-idle").value);
 if(!Number.isFinite(charge)||!Number.isFinite(idle)||charge<1||charge>10||idle<2.5||idle>30)throw new Error("Choose 1 to 10 s while charging and 2.5 to 30 s otherwise.");
 if(idle<charge)throw new Error("The limit while idle cannot be tighter than the limit while charging.");
 await save("/api/freshness",{revision:state.freshness_revision,charge_s:charge,idle_s:idle});
 message("Saved. The planner will replan; the controller picks it up from the next plan.");
}));
el("min-soc-form").addEventListener("submit",action(async()=>{
 const percent=Number(el("min-soc").value);
 if(!Number.isFinite(percent)||percent<5||percent>20)throw new Error("Choose a minimum level between 5% and 20%.");
 await save("/api/min-soc",{revision:state.min_soc_revision,value:Math.round(percent)/100});
 message("Minimum level saved. The planner will replan; the controller picks it up from the next plan.");
}));
el("providers-form").addEventListener("submit",action(async()=>{const expiry=el("eligibility-until").value;if(expiry && !/(Z|[+-]\d\d:\d\d)$/.test(expiry))throw new Error("Eligibility expiry must include UTC Z or an explicit offset.");const until=expiry ? Date.parse(expiry)/1000 : null;if(expiry && !Number.isFinite(until))throw new Error("Check eligibility expiry.");await save("/api/providers",{revision:formRevisions.providers,config:{weather_enabled:el("weather-enabled").checked,prices_enabled:el("prices-enabled").checked,account_enabled:el("account-enabled").checked,product:el("product").value,tariff:el("tariff-code").value,account:el("account").value,device:el("device").value,household_rate_verified:el("eligibility").checked,eligibility_until:until,eligibility_source:el("eligibility-source").value,billing_rule:el("billing-rule").value}});message("Provider settings saved. The separate worker performs connection checks.");}));
el("key-form")?.addEventListener("submit",action(async()=>{try{await save("/api/octopus-key",{api_key:el("api-key").value});message("API key saved privately. Click Connect Octopus to find your linked EV or charger.");}finally{el("api-key").value="";}}));
el("octopus-connect").addEventListener("click",action(async()=>{await save("/api/octopus-connect",{});message("Read-only Octopus connection queued. The worker will find linked devices; no settings are sent to Octopus.");}));
el("provider-refresh").addEventListener("click",action(async()=>{await save("/api/provider-refresh",{});message("Refresh requested. The worker observes provider request limits.");}));
function utcInput(id){const text=el(id).value;if(!text)return null;if(!/(Z|[+-]\d\d:\d\d)$/.test(text))throw new Error("Expiry must include UTC Z or an explicit offset.");const t=Date.parse(text)/1000;if(!Number.isFinite(t))throw new Error("Check expiry date.");return t;}
el("tariff-form").addEventListener("submit",action(async()=>{
 const day=Number(el("day").value),night=Number(el("cheap").value);
 const changed=!state.tariff || day!==state.tariff.day_pence || night!==state.tariff.cheap_pence;
 await save("/api/tariff",{revision:formRevisions.tariff,tariff:{day_pence:day,cheap_pence:night,
  valid_from:Date.now()/1000,valid_until:utcInput("tariff-until"),start_minute:minute(el("start").value),
  end_minute:minute(el("end").value),name:"Intelligent Octopus Go",
  source:changed ? "User-entered account prices" : state.tariff.source,
  confirmed:el("tariff-confirmed").checked}});
 message("Fallback tariff saved. Costs based on these settings are estimates.");
}));

startPolling();
el("calibration-form").addEventListener("submit",action(async()=>{
 const days=Number(el("calibration-days").value);
 if(!Number.isInteger(days)||days<3||days>14)throw new Error("Choose a whole number of days between 3 and 14.");
 await save("/api/calibration",{revision:state.calibration_revision,days});
 message("Saved. The planner will replan.");
}));
