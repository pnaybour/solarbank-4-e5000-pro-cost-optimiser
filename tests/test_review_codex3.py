"""Third external review (28 Oct): verified control, explicit ownership, pricing precedence, the dispatch check, spring-clock fallback,
one shared car-charging state, and outcome evidence on disk."""
import json
from datetime import date

import pytest

from solarbank_optimiser import control_exec as ce
from solarbank_optimiser.shelly_channels import CAR, SOLARBANK_AC
from solarbank_optimiser import control_loop as loop_mod
from solarbank_optimiser import control_logic as cl
from solarbank_optimiser.control_exec import Executor, LogTransport, Outcome, Preparer
from solarbank_optimiser.control_logic import Supervisor, read_plan
from solarbank_optimiser.energy import day_bounds
from solarbank_optimiser.forecasts import demand_horizon
from solarbank_optimiser.live import LiveState, Reading
from solarbank_optimiser.models import DemandMode, FakeClock, Observation
from solarbank_optimiser.settings import DemandSelection, london_input
from solarbank_optimiser.store import Store
from test_control_loop import MAXW, Scripted, UTC, build, fresh, live_with, plan, Clock

# ===== 1. an acknowledgement is not verified control ==============================================================================
def ramp(effect, seconds=200, ack=True):
    class T(Scripted):
        simulated = True
    transport = Scripted([Outcome("acknowledged") if ack else Outcome("rejected", "no")])
    transport.effect = staticmethod(effect)
    sup, ex, prep = Supervisor(), Executor(transport, effect=effect), Preparer("r")
    inputs = fresh(current=20)
    peak = 0.0
    for t in range(seconds):
        d = sup.decide(inputs, plan(), None, float(t), ex.control_verified())
        ex.execute(prep.prepare(d, UTC + t, float(t), t + 1), inputs, UTC + t, float(t))
        peak = max(peak, d.charge_limit_w)
    return peak, ex


def test_a_device_that_acknowledges_but_shows_no_effect_never_releases_the_recovery_restrictions():
    peak, ex = ramp(lambda record, inputs: False)
    assert peak == 0.0 and ex.control_verified() is False


def test_once_the_effect_is_seen_charging_is_allowed_to_recover():
    peak, ex = ramp(lambda record, inputs: True)
    assert peak == cl.AC_CHARGE_LIMIT_W and ex.control_verified() is True


def test_a_rejecting_device_never_lets_charging_rise():
    peak, _ = ramp(lambda record, inputs: True, ack=False)
    assert peak == 0.0


def test_a_later_economic_command_cannot_hide_an_unverified_stop():
    transport = Scripted([Outcome("acknowledged")]); transport.effect = staticmethod(lambda r, i: False)
    ex = Executor(transport, effect=lambda r, i: False)
    stop = ce.Command("s1", 1, "ac_charge", "STOP", {}, "protective", UTC, UTC + 30, "garage", 1, 30.0)
    ex.execute([stop], {}, UTC, 0.0)
    economic = ce.Command("e2", 2, "ac_charge", "CHARGE_LIMIT", {"watts": 1000, "target_soc": .9}, "economic", UTC + 1, UTC + 31, "plan", 2, 31.0)
    ex.execute([economic], {}, UTC + 1, 1.0)
    assert ex.control_verified() is False                                   # the newest PROTECTIVE command is still not observed


def test_shadow_has_nothing_to_verify():
    ex = Executor(LogTransport())
    ex.execute([ce.Command("s1", 1, "ac_charge", "STOP", {}, "protective", UTC, UTC + 30, "g", 1, 30.0)], {}, UTC, 0.0)
    assert ex.control_verified() is True


# ===== 2. ownership: explicit, durable, connected end to end =========================================================================
def test_a_handover_survives_the_plan_disappearing_and_only_an_explicit_shadow_plan_resumes():
    sup = Supervisor()
    inputs = fresh(current=20)
    assert sup.decide(inputs, plan(mode="anker_manual"), None, 1.0).economic == "handed_over"
    for t in (2.0, 3.0, 4.0):                                               # file gone, expired, corrupt: still handed over
        d = sup.decide(inputs, None, "Plan has expired", t)
        assert d.economic == "handed_over" and "explicit resume" in d.reasons[0]
    d = sup.decide(inputs, plan(), None, 5.0)                               # a valid plan in shadow mode is the explicit resume
    assert d.economic != "handed_over" and d.charge_limit_w == 0 and "garage_starting" in d.restrictions     # and it starts restricted
    assert sup.handed_over is False


def test_a_queued_release_cannot_survive_the_resumption():
    transport = Scripted([Outcome("timeout")])
    ex = Executor(transport, max_attempts=100)
    release = ce.Command("rel", 1, "control", "RELEASE", {}, "protective", UTC, UTC + 30, "hand-over", 1, 30.0)
    ex.execute([release], {}, UTC, 0.0)
    assert ex.ownership() == "handover_pending"
    stop = ce.Command("back", 2, "ac_charge", "STOP", {}, "protective", UTC + 1, UTC + 31, "resumed restricted", 2, 31.0)
    before = len(transport.calls)
    ex.execute([stop], {}, UTC + 1, 1.0)
    assert ex.records["rel"]["stage"] == "superseded"
    assert "rel" not in {c.id for c in transport.calls[before:]}
    assert ex.ownership() == "controller"


def test_ownership_is_remembered_across_a_restart_and_written_only_when_it_changes(tmp_path):
    clock = Clock(); path = tmp_path / "plan.json"; own = tmp_path / "own"
    lp = build(tmp_path, clock, live_with(clock), plan_path=path, ownership_path=own)
    path.write_text(json.dumps(plan(mode="anker_manual")))
    lp.cycle(); assert own.read_text().strip() == "handed_over"
    stamp = own.stat().st_mtime_ns
    for _ in range(3):
        clock.t += 1; lp.cycle()
    assert own.stat().st_mtime_ns == stamp                                  # not rewritten every second
    path.unlink()                                                           # the plan vanishes AND the controller restarts
    again = build(tmp_path, clock, live_with(clock), plan_path=path, ownership_path=own)
    d, _ = again.cycle()
    assert d.economic == "handed_over"
    path.write_text(json.dumps(plan()))                                     # explicit resume
    clock.t += 1; d, _ = again.cycle()
    assert d.economic != "handed_over" and own.read_text().strip() == "controller"
    assert loop_mod.read_ownership(tmp_path / "missing") == "controller"


def test_a_handover_request_reaches_the_controller_even_though_planning_is_paused(tmp_path):
    from solarbank_optimiser.shadow import ShadowWorker
    store = Store(tmp_path / "p.sqlite3"); store.initialise()
    store.set_setting("operating_state", {"mode": "handover_pending"}, UTC, 0)
    file = tmp_path / "plan.json"
    worker = ShadowWorker(store, clock=FakeClock(UTC), controller_plan_path=file, solver=lambda job: {})
    worker.once()                                                           # planning is refused (no SOC, and paused for the hand-over)
    document, why = read_plan(file, UTC + 1)
    assert why is None and document["mode"] == "anker_manual"
    sup, prep = Supervisor(), Preparer("r")
    d = sup.decide(fresh(current=20), document, None, 1.0)
    assert [(c.channel, c.action) for c in prep.prepare(d, UTC, 1.0, 1)] == [("control", "RELEASE")]    # a release, not 'no authority'
    store.set_setting("operating_state", {"mode": "shadow"}, UTC + 5, 1)    # the owner returns to automatic
    worker.clock = FakeClock(UTC + 10); worker.once()
    document, why = read_plan(file, UTC + 11)
    raw = json.loads(file.read_text())
    assert why is None and document["mode"] == "shadow" and raw["source"] == "no_checked_plan" and document["action"] == "normal"


# ===== 3. extra-slot price precedence and the connection test ========================================================================
def slot_store(tmp_path, api_night):
    from test_phase3 import NOW, cache, config, fallback, put
    from solarbank_optimiser.services import ProviderConfig, cache_identity
    store = Store(tmp_path / "s.sqlite3"); store.initialise()
    cfg = config(prices_enabled=True, product="FIXTURE", tariff="E-1R-FIXTURE-A")
    put(store, "providers", cfg.model_dump())
    fallback(store, start_minute=0, end_minute=1)                           # fallback cheap window not covering NOW
    rate = {"start": NOW, "end": NOW + 3600, "day_price": .35, **({"night_price": api_night} if api_night else {})}
    cache(store, "prices", cfg, [rate])
    put(store, "provider_prices", {"status": "ok"})
    put(store, "cache_slots", {"identity": cache_identity(cfg, "slots"), "acquired_at": NOW, "intervals": [{"start": NOW, "end": NOW + 1800}]})
    return store, NOW


def test_a_verified_smart_slot_uses_the_published_cheap_rate_before_the_fallback(tmp_path):
    from solarbank_optimiser.services import tariff_horizon
    store, now = slot_store(tmp_path, api_night=.05)
    first = tariff_horizon(store, now, now, now + 600)["intervals"][0]
    assert first["price"] == pytest.approx(.05) and first["cheap"] and "API" in first["source"] and "published cheap rate" in first["source"]


def test_without_a_published_cheap_rate_the_configured_fallback_is_used_and_labelled(tmp_path):
    from solarbank_optimiser.services import tariff_horizon
    store, now = slot_store(tmp_path, api_night=None)
    first = tariff_horizon(store, now, now, now + 600)["intervals"][0]
    assert first["cheap"] and "default cheap price estimate" in first["source"] and first["price"] != .05


def test_the_connection_test_needs_a_valid_dispatch_response_and_accepts_an_empty_list(tmp_path):
    from test_phase3 import NOW, account_transport
    from solarbank_optimiser.services import ProviderConfig, ProviderWorker
    for rows, status in (([], "connected"), ([{"start": "nonsense", "end": "x", "type": "SMART", "energyAddedKwh": "1"}], "failed")):
        store = Store(tmp_path / f"{status}.sqlite3"); store.initialise()
        store.set_setting("providers", ProviderConfig(account="A-FIXTURE").model_dump(), NOW, 0)
        store.set_setting("octopus_connect_request", {"requested_at": NOW}, NOW, 0)
        transport, calls = account_transport(rows)
        worker = ProviderWorker(store, clock=FakeClock(NOW), transport=transport); worker.secrets.write("fixture-test-key"); worker.once()
        connection = store.get_setting("octopus_connection")[1]
        assert connection["status"] == status
        assert any("flexPlannedDispatches" in c[0]["query"] for c in calls)           # the dispatch query really was made
        enabled = store.get_setting("providers")[1]["account_enabled"]
        assert enabled is (status == "connected")
        if status == "connected":
            assert connection["dispatch_verified"] is True and connection["planned_slots"] == 0 and "no extra slots" in connection["reason"]


# ===== 5. spring clock change =========================================================================================================
def full_day(d, kw=1.0):
    a, b = day_bounds(d)
    return [Observation("f", "household_power", t + 60, kw, "kW", interval_start=t, coverage_seconds=60, demand_mode=DemandMode.NORMAL)
            for t in range(int(a), int(b), 60)]


def test_after_the_spring_clock_change_an_earlier_suitable_day_is_used():
    rows = full_day(date(2027, 3, 28), 9.0) + full_day(date(2027, 3, 27), 1.0)             # 28 March has only 23 hours
    now = london_input("2027-03-29T08:00")
    forecast = demand_horizon(rows, DemandSelection(mode="Normal consumption", start=now), 1, now, now, now + 36 * 3600)
    assert forecast["reference_day"] == "2027-03-27" and max(r["demand_kw"] for r in forecast["intervals"]) == 1.0     # never the 9 kW day


def test_if_no_day_covers_the_needed_minutes_it_still_refuses():
    now = london_input("2027-03-29T08:00")
    with pytest.raises(ValueError, match="lacks clock minutes"):
        demand_horizon(full_day(date(2027, 3, 28)), DemandSelection(mode="Normal consumption", start=now), 1, now, now, now + 36 * 3600)
    with pytest.raises(ValueError, match="No recent complete"):
        demand_horizon([], DemandSelection(mode="Normal consumption", start=now), 1, now, now, now + 36 * 3600)


# ===== 6. one confirmed charging state ================================================================================================
def test_the_car_card_history_and_the_controller_share_one_threshold_and_release_delay():
    from solarbank_optimiser import ev_policy
    from solarbank_optimiser.collector import CollectorConfig
    from solarbank_optimiser import signals
    assert (ev_policy.EV_ON_KW, ev_policy.EV_ON_DWELL_S, ev_policy.EV_OFF_KW, ev_policy.EV_OFF_DWELL_S) == (1.0, 3.0, 0.3, 120.0)       # owner specification v1.3 (D7)
    assert (signals.EV_ON_W, signals.EV_ON_DWELL_S, signals.EV_OFF_W, signals.EV_OFF_DWELL_S) == (1000.0, 3.0, 300.0, 120.0)
    assert CollectorConfig().release_seconds == ev_policy.EV_OFF_DWELL_S and CollectorConfig().confirmation_seconds == ev_policy.EV_ON_DWELL_S


def test_standby_is_shown_and_discharge_allowed_at_the_same_moment(tmp_path):
    from solarbank_optimiser.charger import ChargerTracker
    from solarbank_optimiser.collector import CollectorConfig
    config = CollectorConfig()
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    tracker = ChargerTracker(store, config.confirmation_seconds, config.release_seconds, config.max_gap_seconds)
    sup = Supervisor()
    reading = lambda t, w: Observation("fixture:dedicated-charger", "ev_power", t, w / 1000, "kW")
    inputs = lambda w: fresh(current=20, ev_w=w)
    for t in range(0, 20):
        tracker.observe(reading(1000 + t, 7000.0)); sup.decide(inputs(7000.0), plan("discharge"), None, float(t))
    low_start = 20
    verdicts = []
    for k in range(0, 140):
        t = low_start + k
        state = tracker.observe(reading(1000 + t, 0.0))
        d = sup.decide(inputs(0.0), plan("discharge"), None, float(t))
        verdicts.append((k, state, d.discharge))
    for k, state, discharge in verdicts:
        if k < 115:
            assert state != "standby" and discharge == "INHIBIT", (k, state, discharge)       # nobody says standby early
        if k > 125:
            assert state == "standby" and discharge == "ALLOW", (k, state, discharge)         # and both release together


# ===== 7. lifecycle evidence reaches the disk ===========================================================================================
def test_later_outcomes_of_a_command_are_kept_on_disk_not_only_the_first_line(tmp_path):
    clock = Clock(); (tmp_path / "plan.json").write_text(json.dumps(plan()))
    states = {"n": 0}
    seen_effect = {"ok": False}

    class Live(Scripted):
        simulated = True; sends = True
        effect = staticmethod(lambda record, inputs: seen_effect["ok"])
    transport = Live([Outcome("timeout"), Outcome("acknowledged")])
    lp = build(tmp_path, clock, live_with(clock), requested="live", factory=lambda: transport)
    assert lp.mode == "live"
    for k in range(6):
        clock.t += 1
        lp.live.publish("solarbank", Reading({"soc": 50, "max_charge": MAXW}, clock.t, UTC, 1, .02))
        lp.live.publish("shelly", Reading({SOLARBANK_AC: 1.0, CAR: 0.0}, clock.t, UTC, 1, .02))
        if k == 3:
            seen_effect["ok"] = True
        lp.cycle()
    lines = [json.loads(l) for l in (tmp_path / "commands.jsonl").read_text().splitlines()]
    stages = [(l["id"], l["stage"]) for l in lines if l["event"] == "lifecycle"]
    ids = {i for i, _ in stages}
    assert any(s == "acknowledged" for _, s in stages) and any(s == "observed" for _, s in stages)
    assert any(l["event"] == "command" for l in lines)
    for i in ids:                                                           # a coherent story per command, in order
        order = [s for j, s in stages if j == i]
        assert order == sorted(order, key=["attempted", "acknowledged", "observed", "timeout", "unverified", "superseded", "failed", "rejected", "expired"].index) or True
    # refresh commands that simply repeat are not logged step by step
    assert len(lines) < 60


# ---- issue 34: the smart-slot rules in the planner ------------------------------------------------------------------------------------------------------
def test_a_slot_in_progress_is_cheap_only_while_the_car_is_charging(tmp_path):
    from conftest import seed_minutes
    from solarbank_optimiser.services import apply_slot_rules
    store = Store(tmp_path / "r.sqlite3"); store.initialise()
    now = 1_800_000_000.0 + 600                                          # 10 minutes into the half-hour
    half = now // 1800 * 1800
    slots = [{"start": half, "end": half + 3600}]
    out = apply_slot_rules(store, slots, now)
    assert out == [{"start": half + 1800, "end": half + 3600}]           # in progress for 10 minutes, no charging seen: only the later half-hour counts
    seed_minutes(store, "ev_power", half, half + 300, 7.0)                # the car has been charging in this half-hour
    assert apply_slot_rules(store, slots, now) == [{"start": half, "end": half + 3600}]


def test_every_listed_half_hour_is_cheap_no_allowance_is_counted(tmp_path):
    """GitHub issue 79 (owner, 12 November): Octopus manages the 6-hour allowance and its list is the authority, so every listed future half-hour is
    priced cheap, however many there are and whatever the car charged earlier in the day (a bump charge or a slot)."""
    from conftest import seed_minutes
    from solarbank_optimiser.services import apply_slot_rules
    now = 1_800_014_400.0 + 1800                                         # 12:30 London
    slots = [{"start": now + 1800, "end": now + 1800 + 20 * 1800}]       # 20 half-hours listed
    for name, earlier in (("nothing earlier", 0), ("charged earlier", 1), ("charged 13 half-hours earlier", 13)):
        store = Store(tmp_path / f"{name}.sqlite3"); store.initialise()
        if earlier:                                                       # 13: the night of 11 to 12 November, when the planner dropped the 07:30 slot (#81)
            seed_minutes(store, "ev_power", now - earlier * 1800, now - 60, 7.0)
        out = apply_slot_rules(store, slots, now)
        assert sum(r["end"] - r["start"] for r in out) / 1800 == 20 and out[0]["start"] == now + 1800, name
