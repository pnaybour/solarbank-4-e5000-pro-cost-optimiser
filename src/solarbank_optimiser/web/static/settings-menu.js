"use strict";
/* Settings as the Anker app lays them out (owner, 19 November 2026): a menu of grouped rows, each with its current value and an arrow, opening one screen at
   a time with a back arrow. The forms themselves are unchanged (settings.js still fills and saves them); this only chooses which one is shown. */
const SETTINGS_MENU={
 "battery-level":s=>s.min_soc!=null ? Math.round(s.min_soc*100)+"% minimum" : "",
 calibration:s=>s.calibration && s.calibration.max_days ? "full every "+s.calibration.max_days+" days" : "",
 electricity:s=>s.tariff ? Number(s.tariff.day_pence).toFixed(2)+"p · "+Number(s.tariff.cheap_pence).toFixed(2)+"p" : "not set",
 providers:s=>s.api_key_configured ? "Octopus key saved" : "no Octopus key",
 "demand-learning":s=>s.demand_learning ? (s.demand_learning.enabled===false ? "off" : "on") : "",
 planning:s=>s.shadow_plan && s.shadow_plan.status==="checked_shadow" ? "plan ready" : "",
 realtime:s=>s.realtime ? ({safe:"safe",watch:"watch",unsafe:"not safe"}[s.realtime.level]||"")+(s.realtime.controller && s.realtime.controller.present ? " · "+s.realtime.controller.mode : "") : "",
 automation:s=>s.automation ? (s.automation.paused ? "paused" : s.automation.override ? "override" : "automatic") : "",
 control:()=>"",household:s=>s.active_demand ? s.active_demand.split(" ")[0] : "",forecast:()=>"",
 devices:()=>"",freshness:s=>s.freshness && s.freshness.charge_s!=null ? s.freshness.charge_s+" s / "+s.freshness.idle_s+" s" : ""};
function showSettings(){
 const id=location.hash.slice(1),page=id && document.getElementById(id);
 const open=page && page.parentElement && page.parentElement.classList.contains("settings-pages") ? page : null;
 document.body.classList.toggle("setting-open",!!open);
 for(const section of document.querySelectorAll(".settings-pages>section"))section.classList.toggle("open",section===open);
 if(open)window.scrollTo(0,0);
}
function settingsValues(){
 if(typeof state==="undefined" || !state)return;
 for(const [id,value] of Object.entries(SETTINGS_MENU)){const node=document.getElementById("menu-"+id);if(node){try{node.textContent=value(state);}catch(error){node.textContent="";}}}
}
document.addEventListener("DOMContentLoaded",()=>{
 document.body.classList.add("settings-menu-on");
 for(const section of document.querySelectorAll(".settings-pages>section")){
  const back=document.createElement("a");back.href="#";back.className="back";back.textContent="‹ Settings";
  back.addEventListener("click",event=>{event.preventDefault();history.pushState(null,"",location.pathname);showSettings();});
  section.prepend(back);
 }
 window.addEventListener("hashchange",showSettings);showSettings();
 settingsValues();setInterval(settingsValues,2000);
});
