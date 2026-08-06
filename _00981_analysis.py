import sys, json
sys.path.insert(0, r"E:\Github\Contrarian")
import numpy as np
import futu as ft
from app.modules.strategy_config import load_config
from app.modules.screener import evaluate_signals, _build_reason

CODE = "HK.00981"
HOST, PORT = "127.0.0.1", 11111

def rsi(prices, n):
    p = np.asarray(prices, float)
    if len(p) < n + 1:
        return None
    d = np.diff(p)
    g = np.where(d > 0, d, 0.0); l = np.where(d < 0, -d, 0.0)
    ag = np.mean(g[:n]); al = np.mean(l[:n])
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n; al = (al * (n - 1) + l[i]) / n
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + al / ag if ag > 0 else 1e9), 1)

def fnum(v):
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None
    except Exception:
        return None

cfg = load_config()
q = ft.OpenQuoteContext(host=HOST, port=PORT)
out = {}
try:
    ret3, quote = q.get_market_snapshot([CODE])
    ret_k, kd, _ = q.request_history_kline(CODE, ktype=ft.KLType.K_DAY, max_count=260,
                                           fields=[ft.KL_FIELD.ALL])
    pe = turn = chg = amp = None
    if ret3 == ft.RET_OK and quote is not None and not quote.empty:
        r = quote.iloc[0]
        pe = fnum(r.get("pe_ratio")); turn = fnum(r.get("turnover_rate"))
        chg = fnum(r.get("change_rate")); amp = fnum(r.get("amplitude"))
        out["snapshot"] = {
            "name": str(r.get("stock_name")),
            "last_price": fnum(r.get("last_price")),
            "prev_close": fnum(r.get("prev_close")),
            "change_rate": chg,
            "turnover_rate": turn,
            "amplitude": amp,
            "pe_ttm": pe,
            "pb": fnum(r.get("pb_ratio")),
            "volume": int(r.get("volume")) if r.get("volume") is not None else None,
        }
    if ret_k == ft.RET_OK and kd is not None and not kd.empty:
        close = kd["close"].astype(float).tolist()
        high = kd["high"].astype(float).tolist()
        low = kd["low"].astype(float).tolist()
        cur = close[-1]
        ma50 = round(float(np.mean(close[-50:])), 3)
        ma200 = round(float(np.mean(close[-200:])), 3) if len(close) >= 200 else None
        hi52 = max(high[-250:]); lo52 = min(low[-250:])
        drop = round((hi52 - cur) / hi52 * 100, 1)
        pos = round((cur - lo52) / (hi52 - lo52) * 100, 1)
        r14 = rsi(close, 14); r2 = rsi(close[-30:], 2)
        out["tech"] = {
            "bars": len(close), "cur": cur,
            "ma50": ma50, "ma200": ma200,
            "hi52": round(hi52, 3), "lo52": round(lo52, 3),
            "drop_from_high_pct": drop,
            "week52_position_pct": pos,
            "above_ma200": bool(cur > ma200) if ma200 else None,
            "above_ma50": bool(cur > ma50),
            "rsi14": r14, "rsi2": r2,
            "ma50_slope": round((ma50 - float(np.mean(close[-55:-5]))) / float(np.mean(close[-55:-5])) * 100, 2) if len(close) >= 55 else None,
        }
        f = {
            "price": cur, "change_rate": chg, "turnover_rate": turn, "amplitude": amp,
            "pe": pe, "hi52": hi52, "lo52": lo52, "pos_pct": pos,
            "is_leader": False, "hstech_crash": False,
            "sma50": ma50, "sma200": ma200, "rsi2": r2,
        }
        res = evaluate_signals(f, cfg)
        reason = _build_reason(res.get("reason_inputs", {}), res.get("signals", []))
        out["signal"] = {
            "score": res.get("score"), "signals": res.get("signals"),
            "signal_details": res.get("signal_details"),
            "reason": reason,
        }
        out["is_leader"] = False
        out["note"] = "00981 不在龙头观察池：龙头观察池/恒科联动信号不触发；其余信号按真实技术面评估"
    print(json.dumps(out, ensure_ascii=False, indent=2))
except Exception as e:
    import traceback; traceback.print_exc(); print("ERR", repr(e))
finally:
    q.close()
