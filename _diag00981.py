import sys, json
sys.path.insert(0, r"E:\Github\Contrarian")
import futu as ft

CODE = "HK.00981"
HOST, PORT = "127.0.0.1", 11111

# ---- 1) 快照真实字段 ----
q = ft.OpenQuoteContext(host=HOST, port=PORT)
print("==== get_market_snapshot raw ====")
ret, snap = q.get_market_snapshot([CODE])
if ret == ft.RET_OK and snap is not None and not snap.empty:
    row = snap.iloc[0]
    d = {str(k): (None if (v is None or (isinstance(v, float) and v != v)) else v) for k, v in row.items()}
    # 只打关键字段
    keys = ["stock_name","last_price","prev_close","change_rate","turnover_rate","amplitude","pe_ratio","pb_ratio","volume","update_time"]
    for k in keys:
        print(f"  {k} = {d.get(k)}")
    print("  (all columns:", list(snap.columns), ")")
else:
    print("  snapshot ERR", ret, snap)
q.close()

# ---- 2) 全量持仓（含窝轮） ----
print("\n==== position_list_query (ALL, include warrants) ====")
tc = ft.OpenSecTradeContext(filter_trdmarket=ft.TrdMarket.HK, host=HOST, port=PORT)
try:
    ret, pos = tc.position_list_query(trd_env=ft.TrdEnv.REAL, acc_id=0)
    if ret == ft.RET_OK and pos is not None and not pos.empty:
        print("  columns:", list(pos.columns))
        print("  total rows:", len(pos))
        for _, r in pos.iterrows():
            name = str(r.get("stock_name", ""))
            code = str(r.get("code", ""))
            if "00981" in code or "中芯" in name or "SMIC" in name.upper():
                print("  >>> MATCH", code, name,
                      "qty=", r.get("qty"),
                      "cost=", r.get("cost_price"),
                      "mktval=", r.get("market_val"),
                      "pl_ratio=", r.get("pl_ratio"),
                      "pl_val=", r.get("pl_val"),
                      "side=", r.get("position_side"))
            else:
                print("  ", code, name, "qty=", r.get("qty"))
    else:
        print("  pos ERR", ret, pos)
except Exception as e:
    import traceback; traceback.print_exc(); print("  EXC", repr(e))
finally:
    tc.close()
