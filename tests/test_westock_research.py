import pandas as pd

from app.modules import westock_research


class FakeFutuClient:
    def financial_statements(self, code, num=8):
        assert code == "HK.01810"
        return {"report_list": [{
            "date_time_str": "2026-06-30", "period_text": "2026/Q2", "currency_code": "CNY",
            "item_list": [
                {"field_id": 5001, "data": 1000, "yoy": 12},
                {"field_id": 5010, "data": 200, "yoy": 8},
                {"field_id": 5034, "data": 100, "yoy": 5},
                {"field_id": 5051, "data": 80, "yoy": 9},
            ],
        }]}, None

    def analyst_consensus(self, code):
        return {"lowest": 22.7, "average": 42.74, "highest": 75,
                "total": 37, "strong_buy": 75.676, "buy": 16.216,
                "hold": 8.108, "sell": 0, "update_time_str": "2026-08-24"}, None

    def market_snapshot(self, codes):
        return pd.DataFrame([{"code": codes[0], "last_price": 29.02}]), None


def test_futu_research_normalizes_finance_rating_and_consensus(monkeypatch):
    westock_research._CACHE.clear()
    monkeypatch.setattr(westock_research, "_run",
                        lambda _args: {"status": "unavailable", "error": "not installed"})
    monkeypatch.setattr(westock_research.southbound, "holding", lambda _code: ({
        "source": "eastmoney-hsgt", "date": "2026-08-21", "hold_ratio": 19.3,
        "hold_shares": 4_975_874_013, "chg_shares_1d": -54_849_200,
    }, None))

    result = westock_research.get_research("HK.01810", FakeFutuClient())

    assert result["finance"]["status"] == "available"
    assert result["finance"]["periods"][0]["gross_margin"] == 20
    assert result["rating"]["status"] == "available"
    assert result["rating"]["summary"]["forecastInstitutions"] == 37
    assert result["consensus"]["summary"]["targetPriceAvg"] == 42.74
    assert result["south"]["status"] == "available"
