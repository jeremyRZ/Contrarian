from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
df = pd.read_csv(ROOT / ".orb_cache/HK_01810_DAY_2018_2026.csv")
df["time_key"] = pd.to_datetime(df.time_key)
df = df.sort_values("time_key").reset_index(drop=True)
df["ma20"] = df.close.rolling(20).mean()
df["ma60"] = df.close.rolling(60).mean()

# Parameters selected exclusively on pre-2024 data.
start = df.time_key.searchsorted(pd.Timestamp("2024-01-01"))
cash, qty, entry, peak = 20_000.0, 0, 0.0, 0.0
events, equity = [], []
for i in range(start, len(df) - 1):
    row, nxt = df.iloc[i], df.iloc[i + 1]
    if qty == 0 and row.close > row.ma60 and row.ma20 > row.ma60:
        px = nxt.open * 1.0008
        qty = int((cash * .5) // (px * 200)) * 200
        if qty:
            cash -= qty * px * 1.0012
            entry = px
            peak = px
            events.append({"date": nxt.time_key, "side": "BUY", "price": px, "qty": qty})
    elif qty:
        peak = max(peak, row.high)
        reason = "MA20" if row.close < row.ma20 else "TRAIL15" if row.close < peak * .85 else None
        if reason:
            px = nxt.open * .9992
            cash += qty * px * .9988
            events.append({"date": nxt.time_key, "side": "SELL", "price": px,
                           "qty": qty, "reason": reason})
            qty = 0
    equity.append((row.time_key, cash + qty * row.close))

view = df[df.time_key >= "2024-01-01"]
fig, (ax, ax2) = plt.subplots(2, 1, figsize=(17, 10), sharex=True,
                              gridspec_kw={"height_ratios": [3, 1]})
ax.plot(view.time_key, view.close, color="#263238", lw=1, label="Xiaomi close")
ax.plot(view.time_key, view.ma20, color="#ef6c00", lw=1, label="MA20")
ax.plot(view.time_key, view.ma60, color="#1565c0", lw=1.2, label="MA60")
for side, marker, color in (("BUY", "^", "#00a152"), ("SELL", "v", "#d32f2f")):
    es = [e for e in events if e["side"] == side]
    ax.scatter([e["date"] for e in es], [e["price"] for e in es], marker=marker,
               color=color, s=45, label=f"{side} ({len(es)})", zorder=5)
ax.set_title("Xiaomi HK.01810 trend strategy — out-of-sample trades since 2024")
ax.set_ylabel("Adjusted price (HKD)")
ax.grid(alpha=.18); ax.legend(ncol=5)

eq = pd.DataFrame(equity, columns=["date", "equity"])
ax2.plot(eq.date, eq.equity, color="#6a1b9a", lw=1.3, label="Strategy equity")
ax2.axhline(20_000, color="#78909c", lw=.8, ls="--")
ax2.set_ylabel("Equity (HKD)"); ax2.grid(alpha=.18); ax2.legend()
fig.tight_layout()
out = ROOT / "xiaomi_trend_trades_2024.png"
fig.savefig(out, dpi=170, bbox_inches="tight")

pd.DataFrame(events).to_csv(ROOT / "xiaomi_trend_trades_2024.csv", index=False)
print(out)
print(pd.DataFrame(events).to_string(index=False))

# A true daily candlestick view, split by calendar year for readability.
from matplotlib.patches import Rectangle
fig, axes = plt.subplots(3, 1, figsize=(18, 13))
for ax, year in zip(axes, (2024, 2025, 2026)):
    y = view[view.time_key.dt.year == year].reset_index(drop=True)
    for k, r in y.iterrows():
        color = "#d32f2f" if r.close >= r.open else "#00a152"  # HK convention
        ax.vlines(k, r.low, r.high, color=color, lw=.55)
        bottom = min(r.open, r.close)
        height = max(abs(r.close - r.open), .015)
        ax.add_patch(Rectangle((k - .32, bottom), .64, height,
                               facecolor=color, edgecolor=color, lw=.35))
    ax.plot(y.index, y.ma20, color="#ef6c00", lw=1, label="MA20")
    ax.plot(y.index, y.ma60, color="#1565c0", lw=1, label="MA60")
    for e in events:
        if e["date"].year != year: continue
        hit = y.index[y.time_key == e["date"]]
        if len(hit):
            ax.scatter(hit[0], e["price"], marker="^" if e["side"] == "BUY" else "v",
                       color="#00a152" if e["side"] == "BUY" else "#d32f2f",
                       s=48, zorder=6)
    ticks = list(range(0, len(y), max(1, len(y)//8)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([y.iloc[k].time_key.strftime("%Y-%m-%d") for k in ticks], rotation=25)
    ax.set_title(f"{year} — red candle: up, green candle: down")
    ax.set_ylabel("HKD (adjusted)"); ax.grid(alpha=.15)
    ax.legend(loc="upper left")
fig.suptitle("Xiaomi HK.01810 daily candlesticks with trend-strategy trades\n"
             "green triangle = buy, red triangle = sell", fontsize=15)
fig.tight_layout()
candle_out = ROOT / "xiaomi_trend_candles_2024_2026.png"
fig.savefig(candle_out, dpi=180, bbox_inches="tight")
print(candle_out)
