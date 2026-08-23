import pandas as pd

from app.modules import option_mapper


def test_option_mapper_does_not_treat_exit_as_bearish_put():
    result, error = option_mapper.analyze(object(), "HK.01810", "SELL")
    assert error is None
    assert result["action"] == "BLOCKED"
    assert "不映射Put" in result["reason"]


class NoOptions:
    def history_kline(self, *args, **kwargs):
        close = list(range(100, 140))
        return pd.DataFrame({"close": close}), None

    def option_expiration_dates(self, code):
        return None, "没有期权权限"


def test_option_mapper_reports_missing_chain_without_recommendation():
    result, error = option_mapper.analyze(NoOptions(), "HK.00001", "BUY")
    assert result["action"] == "BLOCKED"
    assert error == "没有期权权限"
