import sys, json
import numpy as np
import futu as ft

CODE = "HK.00981"
HOST, PORT = "127.0.0.1", 11111

def rsi(prices, n):
    p = np.asarray(prices, float)
    if len(p) < n + 1:
        return None
    deltas = np.diff(p)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g = np.mean(gains[:n]); avg_l = np.mean(losses[:n])
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 1)

q = ft.OpenQuoteContext(host=HOST, port=PORT)
try:
    # 快照
    ret, snap = q.get_stock_basicinfo(ft.Market.HK, ft.SecurityType.STOCK, [CODE])
    ret2, qd = q.get_rt_ticker(CODE, 1000) if False else (0, None)
    # 真实快照行情
    ret3, quote = q.get_market_snapshot([CODE])
    # K线
    ret_k, kd, _ = q.request_history_kline(CODE, ktype=ft.KLType.K_DAY, max_count=260,
                                           fields=[ft.KL_FIELD.ALL])
    out = {}
    if ret3 == ft.RET_OK and quote is not None and not quote.empty:
        r = quote.iloc[0]
        out["snapshot"] = {
            "name": str(r.get("stock_name")),
            "last_price": float(r.get("last_price")),
            "prev_close": float(r.get("prev_close")),
            "change_rate": round(float(r.get("change_rate")), 2),
            "turnover_rate": float(r.get("turnover_rate")) if r.get("turnover_rate") is not None else None,
            "volume": int(r.get("volume")) if r.get("volume") is not None else None,
            "amplitude": float(r.get("amplitude")) if r.get("amplitude") is not None else None,
        }
    if ret_k == ft.RET_OK and kd is not None and not kd.empty:
        close = kd["close"].astype(float).tolist()
        high = kd["high"].astype(float).tolist()
        low = kd["low"].astype(float).tolist()
        cur = close[-1]
        ma50 = round(float(np.mean(close[-50:])), 3)
        ma200 = round(float(np.mean(close[-200:])), 3) if len(close) >= 200 else None
        hi52 = max(high[-250:]) if len(high) >= 250 else max(high)
        lo52 = min(low[-250:]) if len(low) >= 250 else min(low)
        drop_from_high = round((hi52 - cur) / hi52 * 100, 1)
        pos_pct = round((cur - lo52) / (hi52 - lo52) * 100, 1) if hi52 > lo52 else None
        out["kline"] = {
            "bars": len(close),
            "cur": cur,
            "ma50": ma50, "ma200": ma200,
            "hi52": round(hi52, 3), "lo52": round(lo52, 3),
            "drop_from_high_pct": drop_from_high,
            "week52_position_pct": pos_pct,
            "above_ma200": bool(cur > ma200) if ma200 else None,
            "above_ma50": bool(cur > ma50),
            "rsi14": rsi(close, 14),
            "rsi2": rsi(close[-30:], 2),
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))
except Exception as e:
    print("ERR", repr(e))
finally:
    q.close()
