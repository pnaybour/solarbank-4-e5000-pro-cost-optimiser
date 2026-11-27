"use strict";
/* Real-time diagnostics (owner, 20 November 2026: "put real time on a diagnostic settings page, only keep the pill"). The panel lives on the Settings
   page; the header pill (common.js paintRealtime) stays on every page and links here. Filled from the same /api/status the Settings page polls. */
const STAGE_WORDS={prepared:"Prepared",suppressed:"Suppressed (shadow: nothing was sent)",attempted:"Attempted",acknowledged:"Acknowledged by the device",
 observed:"Observed taking effect",rejected:"Rejected by the device",timeout:"No answer (timed out)",failed:"Failed",unverified:"Acknowledged, effect not seen",
 expired:"Expired unsent",superseded:"Overtaken by a newer command"};
const CTL_STATE={running:"Running",slow:"Slow",stale:"Stale: not reporting",clock:"Clock problem",missing:"Not running"};
function describeCommand(c){
 const names={CHARGE_LIMIT:"Charge up to "+(c.params&&c.params.watts!==undefined ? c.params.watts+" W" : "limit")+(c.params&&c.params.target_soc!=null ? " to "+Math.round(c.params.target_soc*100)+"%" : ""),
  STOP:"Stop AC charging",WITHDRAW:"Withdraw charge request",INHIBIT:"Inhibit battery discharge",ALLOW:"Allow battery discharge"};
 return names[c.action]||c.action;
}
/* What the battery is (or would be) doing, in plain words, from the controller's decision. The three states of the charging sequence (owner, 30 Oct): charging from
   the grid (device mode 3 with a negative power), held idle (mode 3 at 0 W), and handed back to the Anker (mode 0, self-consumption). */
function controllerState(d){
 if(d.economic==="handed_over")return "Handed to the Anker app: the controller sends only the release";
 if(d.charge_limit_w>0)return "Charging from the grid at up to "+Math.round(d.charge_limit_w)+" W"+(d.charge_target_soc!=null ? ", to "+Math.round(d.charge_target_soc*100)+"%" : "")+" (third-party control, mode 3)";
 if(d.discharge==="INHIBIT"){
  const why=d.economic==="hold" ? "the planned level is reached; idle until the cheap window ends" : (d.restrictions||[]).some(r=>r.startsWith("ev_")) ? "car charging" : "protecting";
  return "Held idle: the battery neither charges nor discharges ("+why+"; third-party control at 0 W, mode 3)";
 }
 return "Self-consumption (mode 0): the Anker runs the house and discharges as needed";
}
/* The banner and captions follow the controller's real mode (owner, 12 November 2026: they said "not commissioned" while live).
   One flag, controlLive, set from the controller's status before anything is drawn; the page's fixed wording follows the "control-live" class on <body>. */
function renderController(ctl){
 const box=el("controller");
 if(!ctl){box.hidden=true;return;}
 box.hidden=false;
 const level=ctl.level||"unsafe";
 el("ctl-state").className="rt-pill rt-"+level;
 el("ctl-state").textContent=(RT_ICON[level]||"")+" "+(CTL_STATE[ctl.state]||ctl.state);
 el("ctl-mode").textContent=ctl.present ? (ctl.mode==="live" ? "Live: commands are sent to the battery." : "Shadow mode: every command is prepared and logged, none is sent.")+(ctl.requested && ctl.requested!==ctl.mode ? " Requested: "+ctl.requested+" (not available yet)." : "") : "No status from the controller.";
 el("ctl-heartbeat").textContent=ctl.present ? ctl.heartbeat_age_s.toFixed(1)+" s ago" : "—";
 const d=ctl.decision||{};
 el("ctl-decision").textContent=ctl.present ? (ctl.mode==="live" ? "Now: " : "Would be: ")+controllerState(d) : "—";
 el("ctl-restrictions").textContent=ctl.present && ctl.restrictions.length ? ctl.restrictions.join(", ").replaceAll("_"," ") : (ctl.present ? "none" : "—");
 const p=ctl.plan||{};
 el("ctl-plan").textContent=ctl.present ? (p.valid ? "Valid ("+(p.action||"").replaceAll("_"," ")+(p.target_soc!=null ? " to "+Math.round(p.target_soc*100)+"%" : "")+")" : "None: "+(p.note||"no plan")) : "—";
 const t=ctl.timing_ms||{};
 el("ctl-cycle").textContent=ctl.present ? "#"+ctl.cycle+" · last "+(t.previous_total!=null ? t.previous_total+" ms" : "—")+" · worst "+(t.worst_total!=null ? t.worst_total+" ms" : "—")+" · late "+(ctl.late_cycles??0) : "—";
 const body=el("ctl-commands").querySelector("tbody");body.replaceChildren();
 for(const c of ctl.commands||[]){
  const tr=document.createElement("tr");
  for(const text of [c.channel.replace("_"," "),describeCommand(c),(STAGE_WORDS[c.stage]||c.stage)+(c.simulated ? " [simulated]" : "")]){const td=document.createElement("td");td.textContent=text;tr.appendChild(td);}
  body.appendChild(tr);
 }
 el("ctl-reasons").textContent=ctl.present && ctl.decision_reasons.length ? "Why: "+ctl.decision_reasons.join("; ")+"." : "";
}
function renderRealtime(rt){
 const level=rt ? rt.level : "unsafe";
 el("rt-level").className="rt-pill rt-"+level;
 el("rt-level").querySelector(".rt-icon").textContent=RT_ICON[level];
 el("rt-level-text").textContent=rt ? RT_WORD[level] : "NOT SAFE";
 const panel=el("realtime");                 // change only the border colour: the Settings page shows this screen by an "open" class on the same element
 for(const c of [...panel.classList])if(c.startsWith("rt-border-"))panel.classList.remove(c);
 panel.classList.add("panel","rt-panel","rt-border-"+level);
 el("rt-summary").textContent=rt ? rt.headline+". "+rt.summary : "Running safely cannot be confirmed: the dashboard cannot reach the real-time back end.";
 const list=el("rt-reasons");list.replaceChildren();
 for(const reason of rt ? rt.reasons : []){const li=document.createElement("li");li.className="rt-"+reason.level;li.textContent=(RT_ICON[reason.level])+" "+reason.text;list.appendChild(li);}
 const body=el("rt-devices").querySelector("tbody");body.replaceChildren();
 for(const d of rt ? rt.devices : []){
  const tr=document.createElement("tr");
  const cells=[d.name,d.source,d.age_s===null ? "—" : d.age_s.toFixed(1)+" s ago",d.latency_ms===null ? "—" : d.latency_ms+" ms",d.failures===null ? "—" : String(d.failures),d.state];
  for(const text of cells){const td=document.createElement("td");td.textContent=text;tr.appendChild(td);}
  body.appendChild(tr);
 }
 el("rt-notes").textContent=rt ? rt.notes.join(" ") : "";
 renderController(rt ? rt.controller : null);
}
