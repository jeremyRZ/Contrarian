"""
富途 OpenD 客户端封装
- 懒连接：首次请求时才建立连接
- 端口预检：先用 socket 探活，FutuOpenD 未启动则立即返回，绝不挂起
- 优雅降级：任何失败返回 (None, error_msg)，不抛异常
- 适配 futu-api 10.9（构造器无 trd_env/acc_id，持仓用 position_list_query）
"""
from __future__ import annotations

import os
import socket
from typing import Optional, Tuple

# futu-api 在导入阶段创建日志目录。受限运行环境可能能看到用户 AppData
# 下的目录，却不能可靠执行 exists/makedirs 组合，导致已有目录仍抛
# FileExistsError。只在 SDK 导入期间把日志根目录指向项目内可写位置，
# 随后恢复进程环境；FutuOpenD 的 host/port 与账户配置不受影响。
_original_appdata = os.environ.get("appdata")
_futu_runtime = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".runtime", "futu")
os.makedirs(_futu_runtime, exist_ok=True)
os.environ["appdata"] = _futu_runtime
try:
    import futu as ft
finally:
    if _original_appdata is None:
        os.environ.pop("appdata", None)
    else:
        os.environ["appdata"] = _original_appdata
import yaml
import pandas as pd

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
QUERY_TIMEOUT = 4  # 秒


def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    if not os.path.exists(path):
        alt = os.path.join(os.path.dirname(path), "config.example.yaml")
        path = alt if os.path.exists(alt) else path
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """快速探测 FutuOpenD 端口是否可连，避免 futu-api 内部重连导致挂起。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class FutuClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 11111,
                 trd_env: str = "REAL", acc_id: Optional[str] = None,
                 watchlist_group: str = "Contrarian",
                 accounts: Optional[dict] = None):
        self.host = host
        self.port = port
        self.trd_env = trd_env
        self.acc_id = acc_id
        self.watchlist_group = watchlist_group
        self.accounts = {str(k).upper(): v for k, v in (accounts or {}).items()}
        self._quote = None
        self._trade = None
        self.connected = False
        self.last_error: Optional[str] = None

    # ---------- 连接管理 ----------
    def connect(self) -> Tuple[bool, str]:
        if self.connected and self._quote is not None:
            return True, "connected"
        if not _reachable(self.host, self.port):
            self.connected = False
            self.last_error = "FutuOpenD 未启动或端口不可达"
            return False, self.last_error
        try:
            self._quote = ft.OpenQuoteContext(host=self.host, port=self.port)
            try:
                self._quote.set_sync_query_connect_timeout(QUERY_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass
            ret, _ = self._quote.get_global_state()
            if ret != ft.RET_OK:
                raise RuntimeError("行情连接握手失败")
            self.connected = True
            self.last_error = None
            return True, "connected"
        except Exception as e:  # noqa: BLE001
            self.connected = False
            self._quote = None
            self.last_error = str(e)
            return False, str(e)

    def _ensure_quote(self) -> Tuple[bool, str]:
        if self._quote is None:
            ok, msg = self.connect()
            if not ok:
                return False, msg
        return True, ""

    def _ensure_trade(self) -> Tuple[bool, str]:
        if self._trade is not None:
            return True, ""
        if not _reachable(self.host, self.port):
            return False, "FutuOpenD 未启动或端口不可达"
        try:
            self._trade = ft.OpenSecTradeContext(
                filter_trdmarket=ft.TrdMarket.HK,
                host=self.host, port=self.port,
            )
            try:
                self._trade.set_sync_query_connect_timeout(QUERY_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass
            return True, ""
        except Exception as e:  # noqa: BLE001
            self._trade = None
            return False, f"交易上下文创建失败: {e}"

    def close(self):
        try:
            if self._quote:
                self._quote.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._trade:
                self._trade.close()
        except Exception:  # noqa: BLE001
            pass
        self._quote = None
        self._trade = None
        self.connected = False

    # ---------- 行情 ----------
    def market_snapshot(self, codes) -> Tuple[Optional[object], Optional[str]]:
        ok, msg = self._ensure_quote()
        if not ok:
            return None, msg
        try:
            ret, data = self._quote.get_market_snapshot(list(codes))
            if ret != ft.RET_OK:
                return None, str(data)
            return data, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def stock_basicinfo(self, market=ft.Market.HK,
                        sec_type=ft.SecurityType.STOCK) -> Tuple[Optional[object], Optional[str]]:
        ok, msg = self._ensure_quote()
        if not ok:
            return None, msg
        try:
            ret, data = self._quote.get_stock_basicinfo(market, sec_type)
            if ret != ft.RET_OK:
                return None, str(data)
            return data, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def liquid_stock_candidates(self, *, min_price: float = 2.0,
                                max_lot_price: float = 10_000.0,
                                min_market_value: float = 1_000_000_000.0,
                                limit: int = 300) -> Tuple[Optional[object], Optional[str]]:
        """Server-side first pass over all HK stocks before snapshot requests."""
        ok, msg = self._ensure_quote()
        if not ok:
            return None, msg
        try:
            price = ft.SimpleFilter(); price.stock_field = ft.StockField.CUR_PRICE
            price.filter_min = float(min_price); price.is_no_filter = False
            lot = ft.SimpleFilter(); lot.stock_field = ft.StockField.LOT_PRICE
            lot.filter_max = float(max_lot_price); lot.is_no_filter = False
            cap = ft.SimpleFilter(); cap.stock_field = ft.StockField.MARKET_VAL
            cap.filter_min = float(min_market_value); cap.is_no_filter = False
            cap.sort = ft.SortDir.DESCEND
            rows, begin, total = [], 0, None
            while begin < int(limit):
                ret, data = self._quote.get_stock_filter(
                    market=ft.Market.HK, filter_list=[price, lot, cap],
                    begin=begin, num=min(200, int(limit) - begin))
                if ret != ft.RET_OK:
                    return None, str(data)
                last_page, total, items = data
                for item in items:
                    rows.append({"code": item.stock_code, "name": item.stock_name,
                                 "cur_price": item.cur_price, "lot_price": item.lot_price,
                                 "market_val": item.market_val})
                begin += len(items)
                if last_page or not items:
                    break
            frame = pd.DataFrame(rows)
            frame.attrs["market_filter_total"] = total
            return frame, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def history_kline(self, code: str, ktype=ft.KLType.K_DAY,
                      max_count: int = 250, start: Optional[str] = None,
                      end: Optional[str] = None) -> Tuple[Optional[object], Optional[str]]:
        ok, msg = self._ensure_quote()
        if not ok:
            return None, msg
        try:
            kwargs = {"ktype": ktype, "max_count": max_count}
            if start:
                kwargs["start"] = start
            if end:
                kwargs["end"] = end
            res = self._quote.request_history_kline(code, **kwargs)
            # futu-api 10.x 返回 3 元组 (ret, data, err)；老版本返回 2 元组
            if len(res) == 3:
                ret, data, err = res
            else:
                ret, data = res
                err = None
            if ret != ft.RET_OK:
                return None, str(data) if data is not None else (str(err) if err else "kline 错误")
            return data, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    # ---------- 研究数据（错杀猎手增强） ----------
    def capital_distribution(self, code: str) -> Tuple[Optional[object], Optional[str]]:
        """个股资金流向分布（超大/大/中/小单 净流入/流出）。返回 (DataFrame, error)。

        调用富途 OpenAPI get_capital_distribution(code)，实测字段（futu 10.9.6908,
        HK.00700 2026-07-31）：
          capital_in_super/out_super 超大单、capital_in_big/out_big 大单、
          capital_in_mid/out_mid 中单、capital_in_small/out_small 小单、
          update_time（单位：港元）
        """
        ok, msg = self._ensure_quote()
        if not ok:
            return None, msg
        try:
            ret, data = self._quote.get_capital_distribution(code)
            if ret != ft.RET_OK:
                return None, str(data)
            return data, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def capital_flow(self, code: str, days: int = 10) -> Tuple[Optional[object], Optional[str]]:
        """个股资金流向（主力/特大/大/中/小单净流入）。返回 (DataFrame, error)。"""
        ok, msg = self._ensure_quote()
        if not ok:
            return None, msg
        try:
            from datetime import datetime, timedelta
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=max(int(days), 1))).strftime("%Y-%m-%d")
            ret, data = self._quote.get_capital_flow(
                code, period_type=ft.PeriodType.DAY, start=start, end=end)
            if ret != ft.RET_OK:
                return None, str(data)
            return data, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def buybacks(self, code: str, num: int = 10) -> Tuple[Optional[dict], Optional[str]]:
        """个股回购记录（港股）。返回 (dict{hk_buy_back_list}, error)。"""
        ok, msg = self._ensure_quote()
        if not ok:
            return None, msg
        try:
            ret, data = self._quote.get_corporate_actions_buybacks(code, num=int(num))
            if ret != ft.RET_OK:
                return None, str(data)
            return data, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def news(self, code: str, num: int = 10) -> Tuple[Optional[object], Optional[str]]:
        """个股相关新闻（富途 OpenAPI get_search_news）。

        futu-api 10.9 已用 get_search_news(keyword, max_count) 取代旧的 get_news，
        返回 DataFrame（title/source/publish_time/url/news_sub_type）。以股票代码为
        关键词检索，得到与该标的相关的资讯流。
        """
        ok, msg = self._ensure_quote()
        if not ok:
            return None, msg
        try:
            ret, data = self._quote.get_search_news(code, max_count=int(num))
            if ret != ft.RET_OK:
                return None, str(data)
            return data, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def valuation_detail(self, code: str, valuation_type: int = 1) -> Tuple[Optional[dict], Optional[str]]:
        """个股估值详情（历史 PE/PB/PS 分位、行业/市场排名）。

        调用富途 OpenAPI get_valuation_detail(code, valuation_type)，实测（futu 10.9.6908, HK.00700）：
          valuation_type: 1=PE_TTM 2=PB 3=PS_TTM（4 无效）
          返回 dict（非 DataFrame）：trend.{current_value, valuation_percentile,
          average_value, avg_minus_1_stddev, avg_plus_1_stddev, forward_value}；
          market_distribution.{ranking, total, median_value}；
          plate_distribution.{plate_name, plate_ranking, plate_stock_item_count}
        港股可用。返回 (dict, error)。
        """
        ok, msg = self._ensure_quote()
        if not ok:
            return None, msg
        try:
            res = self._quote.get_valuation_detail(code, valuation_type=int(valuation_type))
            # 富途 10.x 返回 2 元组 (ret, dict)
            if len(res) == 3:
                ret, data, _ = res
            else:
                ret, data = res
            if ret != ft.RET_OK:
                return None, str(data)
            return data, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def shareholders_holding_changes(self, code: str, num: int = 5,
                                     filter_type: int = 1) -> Tuple[Optional[object], Optional[str]]:
        """个股大股东/机构持股变动（增持 filter_type=1 / 减持 filter_type=2）。

        调用富途 OpenAPI get_shareholders_holding_changes(code, num, filter_type)，实测（HK.00700）：
          返回 DataFrame，列含 period_text/name/share_change_num/share_ratio/
          share_ratio_change/holder_type/holding_date_str/share_num
          share_change_num>0 增持、<0 减持；share_ratio 为变动后持股占流通比(%)
        港股可用。返回 (DataFrame, error)。
        """
        ok, msg = self._ensure_quote()
        if not ok:
            return None, msg
        try:
            ret, data = self._quote.get_shareholders_holding_changes(
                code, num=int(num), filter_type=int(filter_type))
            if ret != ft.RET_OK:
                return None, str(data)
            return data, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    # ---------- 交易 / 持仓 ----------
    def acc_list(self) -> Tuple[Optional[object], Optional[str]]:
        ok, msg = self._ensure_trade()
        if not ok:
            return None, msg
        try:
            ret, data = self._trade.get_acc_list()
            if ret != ft.RET_OK:
                return None, str(data)
            return data, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def _trd_env(self):
        return ft.TrdEnv.REAL if str(self.trd_env).upper() == "REAL" else ft.TrdEnv.SIMULATE

    def accinfo(self) -> Tuple[Optional[object], Optional[str]]:
        """账户资金信息（现金/总资产/市值），用于仓位感知。"""
        ok, msg = self._ensure_trade()
        if not ok:
            return None, msg
        env = self._trd_env()
        acc = int(self.acc_id) if self.acc_id else 0
        try:
            ret, data = self._trade.accinfo_query(trd_env=env, acc_id=acc)
            if ret != ft.RET_OK:
                return None, str(data)
            return data, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def cash_ratio(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """返回 (现金比例, 现金, 总资产)；失败返回 (None,None,None)。"""
        data, err = self.accinfo()
        if err or data is None or data.empty:
            return None, None, None
        cols = {c.lower(): c for c in data.columns}
        cash_c = cols.get("cash") or cols.get("total_cash")
        total_c = cols.get("total_assets") or cols.get("total_market_val")
        cash = float(data.iloc[0][cash_c]) if cash_c else None
        total = float(data.iloc[0][total_c]) if total_c else None
        if cash is None or total in (None, 0):
            return None, cash, total
        return round(cash / total * 100, 2), cash, total

    def positions(self) -> Tuple[Optional[object], Optional[str]]:
        ok, msg = self._ensure_trade()
        if not ok:
            return None, msg
        env = self._trd_env()
        acc = int(self.acc_id) if self.acc_id else 0
        try:
            # futu-api 10.9: 用 position_list_query(trd_env=, acc_id=)
            ret, data = self._trade.position_list_query(trd_env=env, acc_id=acc)
            if ret != ft.RET_OK:
                return None, str(data)
            return data, None
        except TypeError:
            # 老版本签名兜底
            try:
                ret, data = self._trade.position_list_query()
                if ret != ft.RET_OK:
                    return None, str(data)
                return data, None
            except Exception as e:  # noqa: BLE001
                return None, str(e)
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def positions_market(self, market: str) -> Tuple[Optional[object], Optional[str]]:
        """Read positions through an isolated HK/CN/US trade context.

        If no account id is configured, discover accounts in that market
        context and select one matching the configured REAL/SIMULATE
        environment.  This keeps the common single-account setup zero-config.
        """
        key = str(market or "").upper()
        if key not in {"HK", "CN", "US"}:
            return None, f"不支持的持仓市场: {market}"
        if not _reachable(self.host, self.port):
            return None, "FutuOpenD 未启动或端口不可达"
        context = None
        try:
            context = ft.OpenSecTradeContext(
                filter_trdmarket=getattr(ft.TrdMarket, key), host=self.host, port=self.port)
            configured = self.accounts.get(key)
            if configured:
                acc = int(configured)
            else:
                ret, accounts = context.get_acc_list()
                if ret != ft.RET_OK:
                    return None, f"无法发现{key}账户: {accounts}"
                if accounts is None or accounts.empty:
                    return None, f"富途未返回可用的{key}账户；请确认账户已开通并登录OpenD"
                candidates = accounts.copy()
                env_col = next((c for c in candidates.columns
                                if str(c).lower() in {"trd_env", "trade_env"}), None)
                if env_col:
                    wanted = str(self.trd_env).upper()
                    matched = candidates[candidates[env_col].astype(str).str.upper().str.contains(wanted)]
                    if not matched.empty:
                        candidates = matched
                id_col = next((c for c in candidates.columns
                               if str(c).lower() in {"acc_id", "account_id"}), None)
                if not id_col:
                    return None, f"{key}账户列表缺少acc_id字段"
                if len(candidates.index) > 1:
                    return None, (
                        f"发现多个{key} {str(self.trd_env).upper()}账户；"
                        f"请在config.yaml的futu.accounts.{key}中指定acc_id"
                    )
                acc = int(candidates.iloc[0][id_col])
            ret, data = context.position_list_query(trd_env=self._trd_env(), acc_id=acc)
            if ret != ft.RET_OK:
                return None, str(data)
            return data, None
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)
        finally:
            try:
                if context: context.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------- 自选股（watchlist） ----------
    # 富途自选股支持通过 modify_user_security 增删，但**仅对用户自建分组有效**；
    # 系统分组（「全部」等）不可写。本模块默认管理一个用户自建分组
    # （watchlist_group，可在 config.yaml 的 futu.watchlist_group 配置，默认 "Contrarian"），
    # 需用户先在富途客户端创建同名用户分组。

    def get_watchlist(self, group: Optional[str] = None) -> Tuple[Optional[list], Optional[str]]:
        """读取富途自选股列表。返回 ([(code, name), ...], error)。"""
        ok, msg = self._ensure_quote()
        if not ok:
            return None, msg
        g = group or self.watchlist_group
        try:
            ret, data = self._quote.get_user_security(g)
            if ret != ft.RET_OK:
                return None, str(data)
            if data is None or data.empty:
                return [], None
            cols = {c.lower(): c for c in data.columns}
            c_code = cols.get("code")
            c_name = cols.get("name") or cols.get("stock_name")
            result = []
            for _, row in data.iterrows():
                code = str(row[c_code])
                name = str(row[c_name]) if c_name else code
                result.append((code, name))
            return result, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def get_watchlist_groups(self) -> Tuple[Optional[list], Optional[str]]:
        """列出所有自选股分组。返回 ([(group_name, group_type), ...], error)。"""
        ok, msg = self._ensure_quote()
        if not ok:
            return None, msg
        try:
            ret, data = self._quote.get_user_security_group()
            if ret != ft.RET_OK:
                return None, str(data)
            if data is None or data.empty:
                return [], None
            result = []
            for _, row in data.iterrows():
                result.append((str(row.get("group_name", "")), str(row.get("group_type", ""))))
            return result, None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def modify_watchlist(self, code: str, action: str = "add",
                         group: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """在富途自选分组中增删一只股票。

        action: 'add' / 'remove'。仅对用户自建分组有效。
        返回 (ok, error_or_none)。分组不存在时富途会返回错误，由调用方提示用户先建分组。
        """
        ok, msg = self._ensure_quote()
        if not ok:
            return False, msg
        g = group or self.watchlist_group
        op = ft.ModifyUserSecurityOp.ADD if str(action).lower() != "remove" else ft.ModifyUserSecurityOp.DEL
        try:
            ret, data = self._quote.modify_user_security(g, op, [code])
            if ret != ft.RET_OK:
                return False, str(data)
            return True, None
        except Exception as e:  # noqa: BLE001
            return False, str(e)


def build_client_from_config(cfg: Optional[dict] = None) -> FutuClient:
    cfg = cfg or load_config()
    f = cfg.get("futu", {})
    return FutuClient(
        host=f.get("host", "127.0.0.1"),
        port=int(f.get("port", 11111)),
        trd_env=f.get("trd_env", "REAL"),
        acc_id=f.get("acc_id") or None,
        watchlist_group=f.get("watchlist_group", "Contrarian"),
        accounts=f.get("accounts") or {},
    )
