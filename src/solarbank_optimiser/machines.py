"""One helper that builds every controller state machine from a declared table. Uses `transitions` 0.9.3 (the one dependency beyond the standard library).

Rules, so that a machine can be read from its table alone:
  * `auto_transitions=False`: only declared events exist. `send_event=True`: guards and callbacks receive an EventData with the keyword arguments.
    `queued=True`: an event fired inside a callback waits until the current transition has finished.
  * Guards are pure. There is no device I/O in any callback. A dwell timer is a `tick` event guarded on the caller's monotonic clock
    (`dwell_at_least`), never a `transitions` timeout thread.
  * `fire()` is the only way the controller sends an event. It checks the event NAME against the declared table first (in 0.9.3 an undeclared
    name raises AttributeError, and catching AttributeError indiscriminately would also hide a programming error inside a guard or callback, so that
    is not done), then catches MachineError for a declared event that is not allowed from the current state. Both cases are logged and counted,
    leave the state unchanged and never crash the loop. An exception raised inside a guard or callback is NOT swallowed.
  * `after_state_change` only appends to the model's `changes` list (the change-log events); writing a file or a database row happens afterwards,
    in the caller, once the transition has completed.
  * `diagram()` renders the same table as Mermaid text, so the picture can never disagree with the code and there is no graphviz dependency."""

from dataclasses import dataclass
import logging

from transitions import Machine, MachineError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Spec:
    """A machine as data: states, the initial state, and transitions as (event, sources, destination[, guard])."""
    name: str
    states: tuple
    initial: str
    transitions: tuple

    def events(self):
        return sorted({t[0] for t in self.transitions})


def dwell_at_least(seconds):
    """A guard for a `tick(now=...)` event: true once the model has been in its state for `seconds` on the caller's monotonic clock."""
    def guard(event):
        now = event.kwargs.get("now")
        return now is not None and getattr(event.model, "entered_at", None) is not None and now - event.model.entered_at >= seconds
    return guard


class Counters:
    """How often events were refused, so the status page can show it. Shared by all machines of one name."""

    def __init__(self):
        self.unknown_event = 0
        self.not_allowed = 0
        self.guard_false = 0

    def as_dict(self):
        return {"unknown_event": self.unknown_event, "not_allowed": self.not_allowed, "guard_false": self.guard_false}


COUNTERS = {}


def counters(name):
    return COUNTERS.setdefault(name, Counters())


def _note_change(event):
    model = event.model
    model.changes.append({"trigger": event.event.name, "from": event.transition.source, "to": event.transition.dest, "now": event.kwargs.get("now")})
    if "now" in event.kwargs:
        model.entered_at = event.kwargs["now"]


def build(spec, model, now=None):
    """Attach a machine for `spec` to `model` (any object). The model gains `state`, `changes`, `entered_at`, `spec` and the event methods."""
    model.changes, model.entered_at, model.spec = [], now, spec
    transitions = []
    for item in spec.transitions:
        event, sources, dest = item[0], item[1], item[2]
        entry = {"trigger": event, "source": list(sources) if isinstance(sources, (list, tuple)) else sources, "dest": dest}
        if len(item) > 3 and item[3] is not None:
            entry["conditions"] = item[3]
        transitions.append(entry)
    model.machine = Machine(model=model, states=list(spec.states), transitions=transitions, initial=spec.initial, auto_transitions=False,
                            send_event=True, queued=True, after_state_change=_note_change, ignore_invalid_triggers=False)
    return model


def fire(model, event, _tick=False, **kwargs):
    """Send `event` to the model. Returns True if the state changed, False if the event was refused (unknown, not allowed here, or a false guard).
    None if it was fired from inside a callback: it is queued and runs once the current transition has finished.
    `_tick=True` marks a periodic dwell check: a false guard is the normal answer there and is not counted."""
    spec, count = model.spec, counters(model.spec.name)
    if event not in spec.events():
        count.unknown_event += 1
        log.error("machine %s: undeclared event %r (state %s)", spec.name, event, model.state)
        return False
    if model.machine._transition_queue:       # called from inside a callback: the event waits its turn, so its outcome is not known yet
        getattr(model, event)(**kwargs)
        return None
    done = len(model.changes)
    try:
        getattr(model, event)(**kwargs)       # a queued machine always answers True, so success is read from the change list, not the answer
    except MachineError as error:
        count.not_allowed += 1
        log.warning("machine %s: event %r not allowed from %s (%s)", spec.name, event, model.state, error)
        return False
    if len(model.changes) == done:
        if not _tick:
            count.guard_false += 1
        return False
    return True


def diagram(spec):
    """Mermaid state diagram text generated from the declared table."""
    lines = [f"%% {spec.name}", "stateDiagram-v2", f"    [*] --> {spec.initial}"]
    for item in spec.transitions:
        event, sources, dest = item[0], item[1], item[2]
        for source in (sources if isinstance(sources, (list, tuple)) else [sources]):
            lines.append(f"    {source} --> {dest}: {event}")
    return "\n".join(lines) + "\n"
