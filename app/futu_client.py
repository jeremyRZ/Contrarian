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

import futu as ft
import yaml

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
                 trd_env: str = "REAL", acc_id: Optional[str] = None):
        self.host = host
        self.port = port
        self.trd_env = trd_env
        self.acc_id = acc_id
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

    def history_kline(self, code: str, ktype=ft.KLType.K_DAY,
                      max_count: int = 250) -> Tuple[Optional[object], Optional[str]]:
        ok, msg = self._ensure_quote()
        if not ok:
            return None, msg
        try:
            res = self._quote.request_history_kline(
                code, ktype=ktype, max_count=max_count)
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


def build_client_from_config(cfg: Optional[dict] = None) -> FutuClient:
    cfg = cfg or load_config()
    f = cfg.get("futu", {})
    return FutuClient(
        host=f.get("host", "127.0.0.1"),
        port=int(f.get("port", 11111)),
        trd_env=f.get("trd_env", "REAL"),
        acc_id=f.get("acc_id") or None,
    )
