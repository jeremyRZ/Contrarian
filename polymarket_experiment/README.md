# Polymarket paper arbitrage experiment

Read-only experiment for the simplest falsifiable strategy: buy equal quantities
of YES and NO only when their executable ask-side cost, taker fees, and a latency
buffer remain below the fixed $1 settlement payout.

The scanner does **not** accept credentials and cannot place orders.

```powershell
python -m polymarket_experiment.scanner --limit 100 --shares 10 --slippage-bps 10
```

Observations are appended to `data/polymarket_observations.jsonl`. The initial
parameters are pre-registered: 10 shares, 10 bps extra latency/slippage buffer,
and markets ranked by current 24-hour volume. Do not tune them against the first
observations; use later data as an out-of-sample check.

An opportunity is only a candidate, not proof of executable profit. REST calls
for the two books are not atomic; `snapshot_skew_ms` records their server-side
timestamp difference. A future live version should use the public WebSocket and
an all-or-none execution policy.
