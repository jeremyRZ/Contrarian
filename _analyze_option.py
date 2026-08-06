import sys, json
sys.path.insert(0, r"E:\Github\Contrarian")
import numpy as np
import futu as ft
from app.modules.strategy_config import load_config
from app.modules.screener import evaluate_signals, _build_reason
from datetime import datetime

UNDER = "HK.00981"
OPT = "HK.SMC260828C80000"
HOST, PORT = "127.0.0.1", 11111
EXP = "2026-08-28"

def fnum(v):
    try:
        if v is None: return None
        f = float(v); return f if f == f else None
    except Exception: return None

def rsi(prices, n):
    p = np.asarray(prices, float)
    if len(p) < n + 1: return None
    d = np.diff(p); g = np.where(d > 0, d, 0.0); l = np.where(d < 0, -d, 0.0)
    ag = np.mean(g[:n]); al = np.mean(l[:n])
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n; al = (al * (n - 1) + l[i]) / n
    if al == 0: return 100.0
    rs = ag / al if al > 0 else 1e9
    return round(100 - 100 / (1 + rs), 1)

cfg = load_config()
q = ft.OpenQuoteContext(host=HOST, port=PORT)
out = {}
try:
    # 底层快照（正确字段名）
    ret, us = q.get_market_snapshot([UNDER])
    if ret == ft.RET_OK and not us.empty:
        r = us.iloc[0]
        last = fnum(r.get("last_price")); prev = fnum(r.get("prev_close_price"))
        chg = round((last - prev) / prev * 100, 2) if (last and prev) else None
        out["underlying"] = {
            "name": str(r.get("name")), "last_price": last, "prev_close": prev,
            "change_rate_today": chg, "turnover_rate": fnum(r.get("turnover_rate")),
            "amplitude": fnum(r.get("amplitude")), "pe_ttm": fnum(r.get("pe_ratio")),
            "update_time": str(r.get("update_time")),
        }
    # 底层 K线技术面
    _, kd, _ = q.request_history_kline(UNDER, ktype=ft.KLType.K_DAY, max_count=260, fields=[ft.KL_FIELD.ALL])
    if not kd.empty:
        close = kd["close"].astype(float).tolist(); high = kd["high"].astype(float).tolist(); low = kd["low"].astype(float).tolist()
        cur = close[-1]; ma50 = np.mean(close[-50:]); ma200 = np.mean(close[-200:]) if len(close) >= 200 else None
        hi52 = max(high[-250:]); lo52 = min(low[-250:])
        out["underlying_tech"] = {
            "ma50": round(ma50, 2), "ma200": round(ma200, 2) if ma200 else None,
            "above_ma200": bool(cur > ma200), "drop_from_high_pct": round((hi52 - cur) / hi52 * 100, 1),
            "week52_pos_pct": round((cur - lo52) / (hi52 - lo52) * 100, 1),
            "rsi14": rsi(close, 14), "rsi2": rsi(close[-30:], 2),
        }
        f = {"price": cur, "change_rate": chg, "turnover_rate": out["underlying"].get("turnover_rate"),
             "amplitude": out["underlying"].get("amplitude"), "pe": out["underlying"].get("pe_ttm"),
             "hi52": hi52, "lo52": lo52, "pos_pct": out["underlying_tech"]["week52_pos_pct"],
             "is_leader": False, "hstech_crash": False, "sma50": ma50, "sma200": ma200, "rsi2": out["underlying_tech"]["rsi2"]}
        res = evaluate_signals(f, cfg)
        out["signal"] = {"score": res.get("score"), "signals": res.get("signals"),
                         "reason": _build_reason(res.get("reason_inputs", {}), res.get("signals", []))}
    # 期权快照（希腊字母/IV/到期）
    reto, os_ = q.get_market_snapshot([OPT])
    if reto == ft.RET_OK and not os_.empty:
        o = os_.iloc[0]
        out["option_quote"] = {
            "name": str(o.get("name")),
            "last_price": fnum(o.get("last_price")),
            "prev_close": fnum(o.get("prev_close_price")),
            "strike": fnum(o.get("option_strike_price")),
            "expiry_distance_days": int(o.get("option_expiry_date_distance")) if o.get("option_expiry_date_distance") is not None else None,
            "delta": fnum(o.get("option_delta")),
            "iv": fnum(o.get("option_implied_volatility")),
            "theta": fnum(o.get("option_theta")),
            "premium": fnum(o.get("option_premium")),
            "contract_size": int(o.get("option_contract_size")) if o.get("option_contract_size") is not None else None,
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))
except Exception as e:
    import traceback; traceback.print_exc(); print("ERR", repr(e))
finally:
    q.close()

# 期权持仓 P&L（单独 trade 上下文）
tc = ft.OpenSecTradeContext(filter_trdmarket=ft.TrdMarket.HK, host=HOST, port=PORT)
try:
    _, pos = tc.position_list_query(trd_env=ft.TrdEnv.REAL, acc_id=0)
    for _, r in pos.iterrows():
        if str(r.get("code")) == OPT:
            print("OPTION_POSITION", json.dumps({
                "qty": r.get("qty"), "cost_price": fnum(r.get("cost_price")),
                "market_val": fnum(r.get("market_val")),
                "pl_ratio_pct": round(fnum(r.get("pl_ratio")) * 100, 2) if r.get("pl_ratio") is not None else None,
                "pl_val": fnum(r.get("pl_val")), "currency": str(r.get("currency")),
                "position_side": str(r.get("position_side")),
            }, ensure_ascii=False))
except Exception as e:
    print("POS_ERR", repr(e))
finally:
    tc.close()
