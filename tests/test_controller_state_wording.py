"""The controller panel says what the battery is doing in plain words (and which device mode that is), not the internal command names."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src/solarbank_optimiser/web/static"
SCRIPT = r"""
const fs=require("fs"),vm=require("vm");
const element=()=>({textContent:"",hidden:false,className:"",dataset:{},classList:{toggle(){}},setAttribute(){},addEventListener(){},querySelector(){return element();},replaceChildren(){},appendChild(){}});
const document={getElementById(){return element();},createElement(){return element();},querySelectorAll(){return [];},createElementNS(){return {setAttribute(){}}}};
const context={document,console,Math,Number,Date,Intl,Object,String,Array,message(){},action:f=>f,save:async()=>{},londonTime:()=>"",startPolling(){},paintRealtime(){},el:()=>element(),RT_ICON:{},RT_WORD:{}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),context);
context.cases=JSON.parse(process.argv[2]);
console.log(JSON.stringify(vm.runInContext("cases.map(controllerState)",context)));
"""


def states(cases):
    out = subprocess.run(["node", "-e", SCRIPT, str(STATIC / "diagnostics.js"), json.dumps(cases)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_each_state_is_named_in_plain_words_with_its_device_mode():
    charging, held, car, normal, handed = states([
        {"economic": "charge_to", "charge_limit_w": 2500, "charge_target_soc": 0.7, "discharge": "ALLOW", "restrictions": []},
        {"economic": "hold", "charge_limit_w": 0, "discharge": "INHIBIT", "restrictions": []},
        {"economic": "discharge_ok", "charge_limit_w": 0, "discharge": "INHIBIT", "restrictions": ["ev_active"]},
        {"economic": "discharge_ok", "charge_limit_w": 0, "discharge": "ALLOW", "restrictions": []},
        {"economic": "handed_over", "charge_limit_w": 0, "discharge": "ALLOW", "restrictions": ["handed_over"]},
    ])
    assert charging == "Charging from the grid at up to 2500 W, to 70% (third-party control, mode 3)"
    assert "Held idle" in held and "idle until the cheap window ends" in held and "mode 3" in held
    assert "car charging" in car and "minimum battery level" not in car      # the controller no longer holds the battery at the minimum level (GitHub issue 64)
    assert normal == "Self-consumption (mode 0): the Anker runs the house and discharges as needed"
    assert "Handed to the Anker app" in handed
    assert "Discharge allow" not in normal                                       # the old wording about internal commands is gone
