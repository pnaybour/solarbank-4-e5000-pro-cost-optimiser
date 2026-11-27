"use strict";
let state;
let formRevisions={demand:0,tariff:0,providers:0};
const el=id=>document.getElementById(id);
const message=(text,error=false)=>{el("message").textContent=text;el("message").classList.toggle("error",error);};
function londonTime(t){return new Date(t*1000).toLocaleString("en-GB",{timeZone:"Europe/London"})+" London";}
function clock(minutes){return String(Math.floor(minutes/60)).padStart(2,"0")+":"+String(minutes%60).padStart(2,"0");}
function minute(value){const [h,m]=value.split(":").map(Number);return h*60+m;}
function optional(id){return el(id).value==="" ? null : Number(el(id).value);}
let lastComputed=0;
const STATUS_TIMEOUT_MS=12000;        // the status page can take a few seconds when the Pi's disk is busy (a backup, a clean-out); it is not unreachable
const FAILURES_BEFORE_UNAVAILABLE=2;   // one slow or failed poll keeps the last picture, which ages and is judged by its own timestamps; two in a row is "unavailable"
async function refresh(loadSettings=false){
 const response=await fetch("/api/status",{signal:AbortSignal.timeout(STATUS_TIMEOUT_MS)});
 if(!response.ok)throw new Error("Dashboard login or connection failed.");
 const data=await response.json();
 if(data.computed_at<lastComputed)return;      // a response older than the one already shown is dropped, never painted over a newer picture
 const limit=data.status_max_age_seconds||30;   // the server's limit, the same one it applies (WP18, #22)
 if(data.stale||(data.served_at-data.computed_at)>limit)throw new Error("Dashboard data is out of date.");
 lastComputed=data.computed_at;state=data;paintRealtime(state.realtime);render(loadSettings);
}
async function save(path,body){
 if(!state)throw new Error("Wait for the page to load.");
 const send=()=>fetch(path,{method:"PUT",headers:{"Content-Type":"application/json","X-CSRF-Token":state.csrf_token},body:JSON.stringify(body),signal:AbortSignal.timeout(5000)});
 let response=await send();
 if(response.status===503){await new Promise(done=>setTimeout(done,1500));response=await send();}      // the database was busy for a moment: try once more
 let result={};
 try{result=await response.json();}catch(error){result={};}
 if(!response.ok)throw new Error(typeof result.detail==="string" ? result.detail : response.status>=500 ? "The server could not complete that request. Please try again." : "Check the entered settings.");
 await refresh(true);
}
function action(fn){return async event=>{event.preventDefault();const button=event.submitter || (event.currentTarget.tagName==="BUTTON" ? event.currentTarget : null);if(button)button.disabled=true;try{await fn();}catch(error){message(error.message,true);}finally{if(button)button.disabled=false;}};}
function startPolling(afterFirst){
 let initial=true,failures=0;
 async function tick(){try{await refresh(initial);failures=0;if(initial){initial=false;if(afterFirst)await afterFirst();}}catch(error){failures+=1;if(initial||failures>=FAILURES_BEFORE_UNAVAILABLE){paintRealtime(null);disconnected();}}finally{setTimeout(tick,3000);}}
 tick();
}

const RT_ICON={safe:"●",watch:"▲",unsafe:"■",unknown:"●"};
const RT_WORD={safe:"SAFE",watch:"WATCH",unsafe:"NOT SAFE",unknown:"CHECKING"};
function paintRealtime(rt){
 // rt === null means the website could not reach its own back end at all, which is itself not safe.
 const level=rt ? rt.level : "unsafe";
 const word=rt ? RT_WORD[level] : "NOT SAFE";
 const pill=el("rt-pill");
 if(pill){
  pill.className="rt-pill rt-"+level;
  el("rt-text").textContent="Real-time: "+word;
  pill.querySelector(".rt-icon").textContent=RT_ICON[level];
  pill.title=rt ? rt.headline+". "+rt.summary : "The dashboard cannot reach the real-time back end.";
 }
}
