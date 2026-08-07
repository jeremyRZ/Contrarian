"""错杀反向信号评分（南向 + 回购 + 新闻 + 资金流向 + 估值分位 + 机构增减持
+ 股息率 + 财报窗口，共八块数据）。

整合 southbound / buybacks / news / capital_flow / fundamentals / dividend /
earnings 八块数据，输出反向信号加分与信号列表，用于加权进「错杀评分模型」。所有外部
调用失败都优雅降级（该项计 0），不影响整体。

权重（可调，集中在此便于维护）：
  1) 南向个股持股（smart money 在买）：...（见下）
  2) 大额回购（公司认为低估）：...（见下）
  3) 利好新闻（关键词情绪，词典法 + 可选 LLM 复核）：...（见下）
  4) 资金流向（机构主力 = 超大单 + 大单净流入，富途逐档）：...（见下）
  5) 估值分位（错杀核心：被低估 = 反弹空间大）：...（见下）
  6) 机构增减持（smart money，含共识度加权）：...（见下）
  7) 股息率 / 分红（港股高股息错杀核心维度，富途快照 TTM 字段）：
       yield >=8% → +1.5 ; >=5% → +1.0 ; >=3% → +0.5 ; 0<yield<3% → +0.2
       增派(ttm>lfy*1.1) → 额外 +0.3
       弃派/削减(ttm=0 但 lfy>0) → -1.0(分红断裂风险)
  8) 财报窗口期（业绩披露前后错杀机会，月份启发式 + 可选东财日历）：
       精确窗口(±14日) → +0.5 ; 财报季(3~4/8~9月) → +0.2 ; 无数据 → 0
总分 clamp 到 [-1.0, 10.0]。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Tuple

from . import southbound, buybacks, news, capital_flow, fundamentals, dividend, earnings, filters


def _within(date_str: Optional[str], days: int) -> bool:
    if not date_str:
        return False
    try:
        d0 = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
    except Exception:
        return False
    return (datetime.now() - d0).days <= days


def reverse_score(client, code: str, days: int = 60, num: int = 10,
                  prefetch: Optional[dict] = None,
                  include_sources: bool = False,
                  ) -> Tuple[Optional[dict], Optional[str]]:
    """计算单票的反向信号评分。返回 (dict, error)。

    dict: {score:float, signals:[str], details:{southbound,buyback,news,...}}
    prefetch: 可选批量预拉取结果 {southbound:{code:(dict,err)}, valuation:{code:(dict,err)}}，
              用于 screener 批量扫描时避免逐只重复调用（见 reverse_score_batch）。
    include_sources: 单票聚合页启用，附带评分过程中已取得的完整源数据；批量扫描默认关闭。
    """
    details: dict = {}
    sources: dict = {}
    signals: list = []
    score = 0.0

    # 1) 南向个股持股（smart money）
    try:
        pf_sb = (prefetch or {}).get("southbound", {}) if prefetch else {}
        if code in pf_sb:
            hd, herr = pf_sb[code]
        else:
            hd, herr = southbound.holding(code)
        if include_sources:
            sources["southbound"] = ({"holding": hd} if hd else {"holding_error": herr}, None)
        if hd:
            upd = int(hd.get("contiguous_up_days") or 0)
            dnd = int(hd.get("contiguous_down_days") or 0)
            cr = hd.get("chg_ratio_1d")
            risk = hd.get("risk") or []
            if upd >= 5:
                s, lab = 2.5, "南向连续增持≥5日"
            elif upd >= 3:
                s, lab = 2.0, "南向连续增持≥3日"
            elif upd == 2:
                s, lab = 1.0, "南向连续增持2日"
            elif upd == 1:
                s, lab = 0.5, "南向当日净增持"
            else:
                s, lab = 0.0, None
            if s > 0:
                signals.append(lab)
                score += s
                if cr is not None and cr >= 0.3:
                    signals.append("南向增持力度强")
                    score += 0.5
            # 反向风险警示（减持 = 错杀反向利空，扣分）
            pen = 0.0
            if dnd >= 5:
                pen, plab = 1.5, "南向连续减持≥5日(风险)"
            elif dnd >= 3:
                pen, plab = 1.0, "南向连续减持≥3日(风险)"
            else:
                plab = None
            if cr is not None and cr <= -5.0:
                pen = max(pen, 1.0)
                plab = "单日南向骤减" + f"{abs(cr):.1f}%(风险)"
            if pen > 0 and plab:
                signals.append(plab)
                score -= pen
            details["southbound"] = {
                "hold_ratio": hd.get("hold_ratio"),
                "contiguous_up_days": upd,
                "contiguous_down_days": dnd,
                "chg_ratio_1d": cr,
                "risk": risk,
                "score": round(s - pen, 1),
            }
        else:
            details["southbound"] = {"error": herr, "score": 0.0}
    except Exception as e:  # noqa: BLE001
        details["southbound"] = {"error": str(e), "score": 0.0}
        if include_sources:
            sources["southbound"] = (None, str(e))

    # 2) 公司回购
    try:
        bb, berr = buybacks.get_buybacks(client, code, num=num)
        if include_sources:
            sources["buybacks"] = (bb, berr)
        if berr:
            details["buyback"] = {"error": berr, "score": 0.0}
        elif bb and bb.get("buybacks"):
            items = bb["buybacks"]
            latest = items[0].get("date") if items else None
            days_ago = None
            if latest:
                try:
                    days_ago = (datetime.now() - datetime.strptime(str(latest)[:10], "%Y-%m-%d")).days
                except Exception:
                    days_ago = None
            recent30 = sum(1 for x in items if _within(x.get("date"), 30))
            if days_ago is not None and days_ago <= 30:
                rec, lab = 2.0, "近30日有大额回购"
            elif days_ago is not None and days_ago <= 60:
                rec, lab = 1.0, "近60日有回购"
            elif days_ago is not None:
                rec, lab = 0.3, "历史有回购记录"
            else:
                rec, lab = 0.3, "有回购记录"
            if recent30 >= 3:
                rec += 0.5
                signals.append("连续多日回购")
            if lab:
                signals.append(lab)
            score += rec
            details["buyback"] = {
                "latest": latest, "days_ago": days_ago,
                "recent30": recent30, "score": round(rec, 1),
            }
        else:
            details["buyback"] = {"score": 0.0}
    except Exception as e:  # noqa: BLE001
        details["buyback"] = {"error": str(e), "score": 0.0}
        if include_sources:
            sources["buybacks"] = (None, str(e))

    # 3) 新闻情绪（细粒度打分，按时间近度加权）
    try:
        nw, nerr = news.get_news(client, code, num=num)
        if include_sources:
            sources["news"] = (nw, nerr)
        if nerr:
            details["news"] = {"error": nerr, "score": 0.0}
        elif nw and nw.get("news"):
            items = nw["news"]
            total = 0.0
            wsum = 0.0
            for i, it in enumerate(items):
                sc = float(it.get("sentiment_score") or 0.0)
                # 近度衰减：越新权重越高（最新 = 1.0，封底 0.5）
                w = max(0.5, 1.0 - 0.05 * i)
                total += sc * w
                wsum += w
            avg = total / wsum if wsum else 0.0
            if avg >= 1.2:
                ns, lab = 1.5, "近期强利好(情绪集中)"
            elif avg >= 0.4:
                ns, lab = 0.75, "近期偏利好"
            elif avg > -0.4:
                ns, lab = 0.0, None
            elif avg > -1.2:
                ns, lab = -0.5, "近期偏利空"
            else:
                ns, lab = -1.0, "近期利空集中"
            if lab:
                signals.append(lab)
            score += ns
            details["news"] = {
                "avg_sentiment": round(avg, 2),
                "items": [{"title": it.get("title"), "sentiment": it.get("sentiment"),
                           "sentiment_score": it.get("sentiment_score")} for it in items[:5]],
                "score": round(ns, 1),
            }
        else:
            details["news"] = {"score": 0.0}
    except Exception as e:  # noqa: BLE001
        details["news"] = {"error": str(e), "score": 0.0}
        if include_sources:
            sources["news"] = (None, str(e))

    # 4) 资金流向（大单/超大单 = 机构主力，富途逐档）
    #    用近N日主力净流入汇总为主信号（反映持续趋势），最新时段快照为辅。
    try:
        cf_dist, cf_derr = capital_flow.distribution(client, code)
        cf_ser, cf_serr = capital_flow.series(client, code, days=min(int(days), 20))
        if include_sources:
            sources["capital_flow"] = ({
                "distribution": cf_dist,
                "distribution_error": cf_derr,
                "flow": cf_ser,
                "flow_error": cf_serr,
            }, None)
        if cf_derr and cf_serr:
            details["capital_flow"] = {"error": cf_derr, "score": 0.0}
        else:
            snap_main = (cf_dist or {}).get("main_net")         # 最新时段主力净流入
            series_main = (cf_ser or {}).get("summary", {}).get("main")  # 近N日主力净流入
            main_ratio = (cf_dist or {}).get("main_ratio")      # 主力占合计比
            # 优先用近N日汇总（趋势更稳），缺则退回快照
            m = series_main if series_main is not None else snap_main
            if m is not None:
                a = abs(m)
                if m > 0:
                    if a >= 1e9:
                        cf_s, lab = 2.0, "主力(超大+大单)持续大额净流入"
                    elif a >= 1e8:
                        cf_s, lab = 1.5, "主力持续净流入"
                    elif a >= 1e7:
                        cf_s, lab = 1.0, "主力净流入"
                    else:
                        cf_s, lab = 0.5, "主力小幅净流入"
                    # 主力占比高 = 机构主导，信号更可信
                    if main_ratio is not None and main_ratio >= 0.6:
                        cf_s += 0.3
                        signals.append("主力占比高(机构主导)")
                else:
                    # 主力净流出 = 错杀反向利空（机构在撤退）
                    if a >= 1e8:
                        cf_s, lab = -1.0, "主力大额净流出(风险)"
                    else:
                        cf_s, lab = -0.5, "主力净流出(风险)"
                if lab:
                    signals.append(lab)
                score += cf_s
                details["capital_flow"] = {
                    "main_net_snapshot": snap_main,
                    "main_net_series": series_main,
                    "main_ratio": main_ratio,
                    "score": round(cf_s, 1),
                }
            else:
                details["capital_flow"] = {"score": 0.0}
    except Exception as e:  # noqa: BLE001
        details["capital_flow"] = {"error": str(e), "score": 0.0}
        if include_sources:
            sources["capital_flow"] = (None, str(e))

    # 5) 估值分位（错杀核心：好公司被低估 = 反弹空间大）
    try:
        val, val_err = fundamentals.valuation_signal(client, code)
        if include_sources:
            sources["fundamentals"] = {
                "valuation": val,
                "valuation_error": val_err,
            }
        if val_err:
            details["valuation"] = {"error": val_err, "score": 0.0}
        elif val:
            vs = val.get("score") or 0.0
            if val.get("label"):
                signals.append(val["label"])
            score += vs
            details["valuation"] = {
                "pe_percentile": val.get("pe", {}).get("percentile"),
                "pb_percentile": val.get("pb", {}).get("percentile"),
                "plate": val.get("plate"),
                "low": val.get("low"),
                "score": round(vs, 1),
            }
        else:
            details["valuation"] = {"score": 0.0}
    except Exception as e:  # noqa: BLE001
        details["valuation"] = {"error": str(e), "score": 0.0}
        if include_sources:
            sources["fundamentals"] = {
                "valuation": None,
                "valuation_error": str(e),
            }

    # 6) 机构增减持（smart money：机构在买 = 错杀反向利好）
    try:
        inst, inst_err = fundamentals.institution_signal(client, code)
        if include_sources:
            sources.setdefault("fundamentals", {})
            sources["fundamentals"].update({
                "institution": inst,
                "institution_error": inst_err,
            })
        if inst_err:
            details["institution"] = {"error": inst_err, "score": 0.0}
        elif inst:
            is_ = inst.get("score") or 0.0
            if inst.get("label"):
                signals.append(inst["label"])
            score += is_
            details["institution"] = {
                "incre_n": inst.get("incre_n"),
                "decre_n": inst.get("decre_n"),
                "consensus": inst.get("consensus"),
                "net_shares": inst.get("net_shares"),
                "score": round(is_, 1),
            }
        else:
            details["institution"] = {"score": 0.0}
    except Exception as e:  # noqa: BLE001
        details["institution"] = {"error": str(e), "score": 0.0}
        if include_sources:
            sources.setdefault("fundamentals", {})
            sources["fundamentals"].update({
                "institution": None,
                "institution_error": str(e),
            })

    # 7) 股息率 / 分红（港股高股息错杀核心维度，富途快照 TTM 字段）
    try:
        dv, dv_err = dividend.dividend_signal(client, code)
        if dv_err:
            details["dividend"] = {"error": dv_err, "score": 0.0}
        elif dv:
            dvs = dv.get("score") or 0.0
            if dv.get("label"):
                signals.append(dv["label"])
            score += dvs
            details["dividend"] = {
                "yield_ratio": dv.get("yield_ratio"),
                "dividend_ttm": dv.get("dividend_ttm"),
                "increased": dv.get("increased"),
                "omitted": dv.get("omitted"),
                "score": round(dvs, 1),
            }
        else:
            details["dividend"] = {"score": 0.0}
    except Exception as e:  # noqa: BLE001
        details["dividend"] = {"error": str(e), "score": 0.0}

    # 8) 财报窗口期（业绩披露前后的错杀机会窗口，月份启发式 + 可选东财日历）
    try:
        es, es_err = earnings.earnings_signal(code)
        if es_err:
            details["earnings"] = {"error": es_err, "score": 0.0}
        elif es:
            ess = es.get("score") or 0.0
            if es.get("label"):
                signals.append(es["label"])
            score += ess
            details["earnings"] = {
                "next_date": es.get("next_date"),
                "days_to": es.get("days_to"),
                "in_season": es.get("in_season"),
                "in_window": es.get("in_window"),
                "available": es.get("available"),
                "score": round(ess, 1),
            }
        else:
            details["earnings"] = {"score": 0.0}
    except Exception as e:  # noqa: BLE001
        details["earnings"] = {"error": str(e), "score": 0.0}

    score = round(max(-1.0, min(10.0, score)), 1)
    result = {"score": score, "signals": signals, "details": details}
    if include_sources:
        # 仅单票聚合接口启用；批量扫描保持轻量响应。
        result["sources"] = sources
    return result, None


def _prefetch(client, codes: list, max_workers: int = 8) -> dict:
    """批量预拉取南向个股持股 + 估值分位（并行 HTTP / FutuOpenD 调用）。

    返回 {southbound:{code:(dict,err)}, valuation:{code:(dict,err)}}，
    reverse_score 通过 prefetch 直接复用，避免扫描时对同一批代码逐只重复调用。
    """
    sb: dict = {}
    val: dict = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(southbound.holding, c): ("sb", c) for c in codes}
        futs.update({ex.submit(fundamentals.valuation_signal, client, c): ("val", c) for c in codes})
        for f in as_completed(futs):
            kind, c = futs[f]
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001
                res = (None, str(e))
            if kind == "sb":
                sb[c] = res
            else:
                val[c] = res
    return {"southbound": sb, "valuation": val}


def reverse_score_batch(client, codes: list, days: int = 60, num: int = 10,
                         max_workers: int = 8) -> dict:
    """批量计算多票反向信号（用于扫描，显著降低墙钟延迟）。

    1) 并行预拉取南向持股 + 估值（每代码各一次）；
    2) 逐票 reverse_score 用预拉取结果，且多票并发执行（覆盖回购/新闻/资金流/机构/沽空）。
    返回 {code: (dict, error)}。
    """
    prefetch = _prefetch(client, codes, max_workers)
    out: dict = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(reverse_score, client, c, days, num, prefetch): c for c in codes}
        for f in as_completed(futs):
            c = futs[f]
            try:
                out[c] = f.result()
            except Exception as e:  # noqa: BLE001
                out[c] = (None, str(e))
    return out
