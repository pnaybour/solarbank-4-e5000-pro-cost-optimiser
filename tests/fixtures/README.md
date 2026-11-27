# Offline provider evidence

`weather-two-plane.json` is a synthetic Open-Meteo-shaped forecast for 25 October
2026. It contains preceding-hour irradiance means, temperature and wind. It is
not a downloaded forecast and must never be used as a real historical issuance.
The fixed location matches the specification. Tests mutate copies for location,
units, missing radiation, interval gaps and insufficient-horizon cases.

Other price, dispatch, account and token responses are constructed explicitly in
`test_phase3.py`; all account identifiers, keys and tokens are synthetic fixtures.
No test connects to equipment or an account. Timeout/rate-limit/connection failures
are injected, and the real transport's total deadline is checked with a fake child.
