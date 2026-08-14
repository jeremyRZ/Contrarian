import pandas as pd

from app.modules import analyze, fundamentals, news


class AnalyzeClient:
    def market_snapshot(self, codes):
        return pd.DataFrame([{
            "code": "HK.02706", "name": "海致科技集团", "last_price": 41.1,
            "change_rate": -5.99, "pe_ratio": -72.36, "pb_ratio": -10.24,
            "highest52weeks_price": 161.6, "lowest52weeks_price": 21.44,
        }]), None

    def history_kline(self, code, max_count=60):
        return pd.DataFrame({"close": [50.0] * 60, "volume": [100.0] * 60}), None


def test_negative_pe_pb_are_not_described_as_undervalued():
    result, err = analyze.analyze(AnalyzeClient(), "HK.02706")

    assert err is None
    assert "估值偏低" not in result["conclusion"]
    assert "亏损" in result["conclusion"]
    assert "净资产" in result["conclusion"]


def test_news_filters_titles_for_other_companies(monkeypatch):
    class Client(AnalyzeClient):
        def news(self, code, num=10):
            return [
                {"title": "海致科技集团：董事会召开日期"},
                {"title": "良信股份获得外观设计专利授权"},
            ], None

    result, err = news.get_news.__wrapped__(Client(), "HK.02706", num=10)

    assert err is None
    assert [item["title"] for item in result["news"]] == ["海致科技集团：董事会召开日期"]
    assert result["filtered_irrelevant"] == 1


def test_negative_valuation_is_a_risk_not_a_low_valuation_signal(monkeypatch):
    class Client:
        def valuation_detail(self, code, valuation_type):
            return {
                "trend": {"current_value": -10, "valuation_percentile": 10, "average_value": -20},
                "plate_distribution": {}, "market_distribution": {},
            }, None

    result, err = fundamentals.valuation_signal.__wrapped__(Client(), "HK.02706")

    assert err is None
    assert result["low"] is False
    assert result["score"] < 0
    assert "不可按低估处理" in result["label"]
