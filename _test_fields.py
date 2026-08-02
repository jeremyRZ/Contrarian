"""临时测试：连接本地 FutuOpenD，比对 buybacks/news 原始返回与归一化逻辑。"""
import importlib.util, json, sys

import futu as ft

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

ROOT = r"E:/Github/Contrarian/app/modules"
buybacks_mod = load(ROOT + "/buybacks.py", "buybacks")
news_mod = load(ROOT + "/news.py", "news")

class Client:
    def __init__(self):
        self.q = ft.OpenQuoteContext(host="127.0.0.1", port=11111)
        try:
            self.q.set_sync_query_connect_timeout(8)
        except Exception:
            pass
    def buybacks(self, code, num=10):
        ret, data = self.q.get_corporate_actions_buybacks(code, num=int(num))
        if ret != ft.RET_OK:
            return None, str(data)
        return data, None
    def news(self, code, num=10):
        ret, data = self.q.get_news(code, num=int(num))
        if ret != ft.RET_OK:
            return None, str(data)
        return data, None

c = Client()
code = "HK.00700"

print("=" * 70)
print("BUYBACKS  raw")
raw, err = c.buybacks(code, num=5)
print("err:", err)
print("raw type:", type(raw))
if isinstance(raw, dict):
    print("raw keys:", list(raw.keys()))
    df = raw.get("hk_buy_back_list")
    print("df type:", type(df))
    if df is not None and hasattr(df, "columns"):
        print("columns:", list(df.columns))
        print(df.head(3).to_string())
elif raw is not None:
    print("raw preview:", str(raw)[:500])

print("-" * 70)
print("BUYBACKS  normalized")
res, e2 = buybacks_mod.get_buybacks(c, code, num=5)
print("err:", e2)
print(json.dumps(res, ensure_ascii=False, default=str)[:2000])

print("=" * 70)
print("NEWS raw")
raw2, err2 = c.news(code, num=5)
print("err:", err2)
print("raw2 type:", type(raw2))
if isinstance(raw2, list):
    print("len:", len(raw2))
    if raw2:
        print("first item type:", type(raw2[0]))
        print("first item keys:", list(raw2[0].keys()) if isinstance(raw2[0], dict) else raw2[0])
        print(json.dumps(raw2[0], ensure_ascii=False, default=str)[:800])
elif hasattr(raw2, "to_dict"):
    print("df columns:", list(raw2.columns))
    print(raw2.head(2).to_string())
else:
    print("raw2 preview:", str(raw2)[:500])

print("-" * 70)
print("NEWS normalized")
res2, e3 = news_mod.get_news(c, code, num=5)
print("err:", e3)
print(json.dumps(res2, ensure_ascii=False, default=str)[:2000])

c.q.close()
