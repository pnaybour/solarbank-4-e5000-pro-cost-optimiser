from copy import deepcopy
import pytest
from solarbank_optimiser.emhass_adapter import validate_native
from solarbank_optimiser.planning import BatteryModel
from dataclasses import asdict


def fixture():
    # A one-hour DC charge of 1 kW stores .86 kWh. It ends exactly at 5%.
    model=asdict(BatteryModel())
    job={'model':model,'initial_soc':.136,'scenarios':[{'intervals':[{'start':0.,'end':3600.,'pv_kw':0.,'demand_kw':.4}]}],
         'prices':[{'price':.066}]}
    result={'status':'optimal','controls_enabled':False,'initial_soc':.136,'flows':[{
        'start':0.,'end':3600.,'soc':.05,'charge_kw':0.,'discharge_kw':.774,
        'inverter_out_kw':.774,'inverter_in_kw':0.,'import_kw':0.,'export_kw':0.,'curtailed_kw':0.}]}
    # Instead use an exactly matched .774 kW household load supplied by battery.
    job['scenarios'][0]['intervals'][0]['demand_kw']=.774
    return job,result


def test_native_independent_balance_and_initial_inventory():
    job,result=fixture()
    assert validate_native(job,result)['independently_checked']


@pytest.mark.parametrize('key',['soc','charge_kw','import_kw','export_kw','inverter_in_kw','curtailed_kw'])
def test_native_tampered_flows_are_rejected(key):
    job,result=fixture();result['flows'][0][key]+=.1
    with pytest.raises(ValueError):validate_native(job,result)


def test_native_initial_soc_and_nonoptimal_result_are_rejected():
    job,result=fixture();result['initial_soc']=.05
    with pytest.raises(ValueError):validate_native(job,result)
    result['status']='optimal_inaccurate'
    with pytest.raises(ValueError):validate_native(job,result)


def test_cli_preserves_virtual_environment_python_symlink(tmp_path,monkeypatch):
    import sys
    from solarbank_optimiser.cli import main
    import solarbank_optimiser.shadow as shadow
    executable=tmp_path/'solver-env'/'bin'/'python'
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    seen=[]
    class Worker:
        def __init__(self,store,emhass,**options):seen.append(emhass.python)
        def run(self,stop,count):assert count==1
    monkeypatch.setattr(shadow,'ShadowWorker',Worker)
    main(['--database',str(tmp_path/'controller.sqlite3'),'shadow','--solver-python',str(executable)])
    assert seen==[executable.absolute()]
    assert seen[0]!=executable.resolve()
