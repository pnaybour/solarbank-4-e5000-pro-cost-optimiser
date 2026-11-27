"""The car-charging signal, defined ONCE (owner specification v1.3 section 5.2; decision D7, 2 November 2026). Standard library only: the controller imports it.

The controller (`signals.EvSignal`), the car card and the charging history (`charger.ChargerTracker`) all use these values, so they cannot disagree:

    charging starts when the car channel is ABOVE 1,000 W for 3 s
    charging has stopped when it is BELOW 300 W for 120 s
    in between (300 to 1,000 W, for example a tapering charge) nothing changes

This replaces the 10 W cutoff and 60 s release delay of specification 8.1 and of the 28 October restart rule. A reading of exactly 1,000 W does not start a session
and exactly 300 W does not end one."""

EV_ON_KW = 1.0
EV_ON_DWELL_S = 3.0
EV_OFF_KW = 0.3
EV_OFF_DWELL_S = 120.0
EV_ON_W, EV_OFF_W = EV_ON_KW * 1000.0, EV_OFF_KW * 1000.0
