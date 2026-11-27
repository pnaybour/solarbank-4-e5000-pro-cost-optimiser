"use strict";
function reviewText(review){
 if(!review)return "not saved";
 const pct=review.error_pct===null||review.error_pct===undefined ? "" : " ("+(review.error_pct>=0 ? "+" : "")+review.error_pct.toFixed(0)+"%)";
 return review.forecast_kwh.toFixed(1)+" forecast, "+review.measured_kwh.toFixed(1)+" measured"+pct;
}
function renderAccuracy(){
 const a=state.savings && state.savings.forecast_accuracy;
 const line=name=>{const x=a && a[name];return x && x.days ? x.days+" day"+(x.days>1 ? "s" : "")+": forecast "+(x.bias_kwh>=0 ? "above" : "below")+" measured by "+Math.abs(x.bias_kwh).toFixed(1)+" kWh a day on average"+(x.mean_abs_error_pct!==null ? " (typical miss "+x.mean_abs_error_pct.toFixed(0)+"%)" : "") : "no day compared yet";};
 el("accuracy-solar").textContent=line("solar");el("accuracy-solar-adjusted").textContent=line("solar_adjusted");el("accuracy-demand").textContent=line("demand");
}
function renderDaily(){
 renderAccuracy();
 const body=el("daily-table").querySelector("tbody");body.replaceChildren();
 const days=state.savings ? state.savings.days : [];
 const pounds=v=>v===null||v===undefined ? "—" : "£"+Number(v).toFixed(2);
 const kwh=v=>v===null||v===undefined ? "—" : Number(v).toFixed(2);
 const hm=t=>t ? new Date(t*1000).toLocaleTimeString("en-GB",{timeZone:"Europe/London",hour:"2-digit",minute:"2-digit"}) : "—";
 for(const d of days){
  const tr=document.createElement("tr");
  for(const text of [d.date,d.complete ? "Yes" : "Partial",kwh(d.consumption_kwh),kwh(d.import_kwh),pounds(d.baseline_cost_gbp),pounds(d.actual_cost_gbp),pounds(d.saving_gbp),
   reviewText(d.forecast_review && d.forecast_review.solar),reviewText(d.forecast_review && d.forecast_review.demand),hm(d.first_full_at),String(d.full_sun_minutes),String(d.throttled_minutes),d.forecast_saved ? kwh(d.possible_lost_kwh)+" ("+kwh(d.evidenced_lost_kwh)+" held back)" : "forecast not saved"]){
   const td=document.createElement("td");td.textContent=text;tr.appendChild(td);}
  body.appendChild(tr);
 }
 if(!days.length){const tr=document.createElement("tr"),td=document.createElement("td");td.colSpan=13;td.textContent="No day has been scored yet.";tr.appendChild(td);body.appendChild(tr);}
}
function render(){renderDaily();el("mode").textContent=state.mode==="observe" ? "Observe only" : state.mode.replaceAll("_"," ");}
function disconnected(){message("Connection unavailable. History could not be refreshed.",true);}
el("history-form").addEventListener("submit",action(async()=>{const query=new URLSearchParams({start:el("from").value,end:el("to").value});const response=await fetch("/history.csv?"+query);if(!response.ok)throw new Error("Check the history date range.");const link=document.createElement("a");link.href=URL.createObjectURL(await response.blob());link.download="solarbank-observations.csv";link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);}));
function carQuery(){return new URLSearchParams({start:el("car-from").value,end:el("car-to").value});}
async function loadCarHistory(){
  const response=await fetch("/api/car-history?"+carQuery());
  if(!response.ok)throw new Error("Check the charging-history date range.");
  const data=await response.json();
  el("car-summary").textContent=(data.charging_kwh===null ? "No covered observations" : data.charging_kwh.toFixed(3)+" charging kWh")+" · "+(data.partial ? "Partial coverage. Unknown periods remain unknown." : "Covered recorder samples.");
  const body=el("car-history-body");body.replaceChildren();
  for(const day of data.daily){const row=document.createElement("tr");for(const value of [day.date,day.charging_kwh===null ? "—" : day.charging_kwh.toFixed(3),day.active_seconds===null ? "—" : (day.active_seconds/60).toFixed(1)+" min",(day.covered_seconds/60).toFixed(1)+" min",day.partial ? "Partial" : "Covered"]){const cell=document.createElement("td");cell.textContent=value;row.append(cell);}body.append(row);}
}
el("car-history-form").addEventListener("submit",action(loadCarHistory));
el("car-csv").addEventListener("click",action(async()=>{const response=await fetch("/car-charging.csv?"+carQuery());if(!response.ok)throw new Error("Check the charging-history date range.");const link=document.createElement("a");link.href=URL.createObjectURL(await response.blob());link.download="car-charging.csv";link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);}));

startPolling(loadCarHistory);

el("half-hours-link").addEventListener("click",action(async()=>{const query=new URLSearchParams({start:el("from").value,end:el("to").value});const response=await fetch("/half-hours.csv?"+query);if(!response.ok)throw new Error("Check the history date range.");const link=document.createElement("a");link.href=URL.createObjectURL(await response.blob());link.download="half-hours.csv";link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);}));
const ANKER_WORDS={grid_charge:"charging from the grid",solar_charge:"charging from solar",discharge:"discharging",idle:"idle",unknown:"unknown"};
async function loadValidation(){
 try{
  const response=await fetch("/api/validation?days=7",{headers:{Accept:"application/json"}});
  if(!response.ok)throw new Error("unavailable");
  const data=await response.json();
  const hm=t=>new Date(t*1000).toLocaleTimeString("en-GB",{timeZone:"Europe/London",hour:"2-digit",minute:"2-digit"});
  const fill=(id,rows)=>{const body=el(id).querySelector("tbody");body.replaceChildren();for(const cells of rows){const tr=document.createElement("tr");for(const text of cells){const td=document.createElement("td");td.textContent=text;tr.appendChild(td);}body.appendChild(tr);}return body;};
  el("validation-summary").textContent=data.minutes_compared ? "Last "+data.days.length+" finished day(s): "+data.agreement_pct.toFixed(1)+"% of "+data.minutes_compared+" compared minutes agree." : "No day has been compared yet. A day is compared shortly after midnight.";
  const days=fill("validation-days",data.days.map(d=>[d.date,d.agreement_pct===null ? "—" : d.agreement_pct.toFixed(1)+"%",String(d.minutes_compared),String(d.disagreeing_minutes),String(d.minutes_without_controller_record),String(d.minutes_without_readings)]));
  if(!data.days.length)fill("validation-days",[["—","","","","",""]]);
  const rows=data.differences.map(d=>[d.date,hm(d.start),hm(d.end),String(d.minutes),data.kinds[d.kind],ANKER_WORDS[d.actual]||d.actual,d.mean_battery_kw.toFixed(2),d.soc_start===null||d.soc_start===undefined ? "—" : Math.round(d.soc_start*100)+"% → "+Math.round(d.soc_end*100)+"%"]);
  fill("validation-differences",rows.length ? rows : [["No differences recorded in this period.","","","","","","",""]]);
 }catch(error){el("validation-summary").textContent="The agreement report is not available right now.";}
}
loadValidation();setInterval(loadValidation,60000);
