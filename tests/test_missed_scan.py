import pandas as pd

from app.modules import missed_scan


class FakeClient:
    def stock_basicinfo(self):
        return pd.DataFrame({"code": ["HK.00001", "HK.00002"]}), None


def test_all_pool_uses_basicinfo_codes_and_minimum_drop(monkeypatch):
    captured = {}

    def fake_screen(client, codes, top_n, hstech_code):
        captured["codes"] = codes
        return {"results": [
            {"code": "HK.00001", "pe": 8, "score": 3, "signals": ["深度超跌反弹"],
             "reason_inputs": {"drop_pct": 25}},
            {"code": "HK.00002", "pe": 8, "score": 3, "signals": ["深度超跌反弹"],
             "reason_inputs": {"drop_pct": 10}},
        ]}, None

    monkeypatch.setattr(missed_scan.screener, "screen", fake_screen)
    monkeypatch.setattr(
        missed_scan.reverse_signals,
        "reverse_score",
        lambda *args, **kwargs: ({"score": 0, "signals": []}, None),
    )

    result, error = missed_scan.missed_scan(FakeClient(), pool="all", min_drop_pct=20)

    assert error is None
    assert captured["codes"] == ["HK.00001", "HK.00002"]
    assert [item["code"] for item in result] == ["HK.00001"]
