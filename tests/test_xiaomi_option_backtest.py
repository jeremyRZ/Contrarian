from pathlib import Path

from app.modules.xiaomi_option_backtest import implied_vol_delta, parse_dtop


def test_parse_dtop_extracts_call_and_put_settlements(tmp_path: Path):
    p = tmp_path / "sample.rpt"
    p.write_text("""EXPIRATION DATE : 28 AUG 26
   30.00   10,145    8,890     1,173   2,161   135   0.38  0.25  6,435  5,310  46  151  26  1.37 -0.91
""", encoding="latin-1")
    result = parse_dtop(p)
    assert result.iloc[0].call_settle == .38
    assert result.iloc[0].put_settle == 1.37


def test_parse_dtop_accepts_dash_in_change_columns(tmp_path: Path):
    p = tmp_path / "sample.rpt"
    p.write_text("""EXPIRATION DATE : 30 JUN 21
   26.00  106 38 - 106 3 2.63 - 275 251 - 275 12 1.50 -
""", encoding="latin-1")
    result = parse_dtop(p)
    assert result.iloc[0].call_settle == 2.63
    assert result.iloc[0].put_settle == 1.50


def test_implied_vol_delta_recovers_call_parameters():
    result = implied_vol_delta(1.31, 29.02, 30, 38, "call")
    assert result is not None
    iv, delta = result
    assert .3 < iv < .7
    assert .3 < delta < .7
