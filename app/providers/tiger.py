from __future__ import annotations

from pathlib import Path

import pandas as pd


class TigerPositionsProvider:
    """Minimal Tiger OpenAPI adapter exposing read-only US positions only."""

    def __init__(self, props_path: str, enabled: bool = True):
        self.props_path = str(props_path or "").strip()
        self.enabled = bool(enabled)

    def positions(self):
        if not self.enabled:
            return None, "Tiger OpenAPI 未启用"
        if not self.props_path:
            return None, "未配置 tiger.props_path"
        path = Path(self.props_path).expanduser()
        config_file = path if path.is_file() else path / "tiger_openapi_config.properties"
        if not config_file.is_file():
            return None, f"找不到 Tiger OpenAPI 配置文件: {config_file}"
        try:
            from tigeropen.common.consts import Market, SecurityType
            from tigeropen.tiger_open_config import TigerOpenClientConfig
            from tigeropen.trade.trade_client import TradeClient

            cfg = TigerOpenClientConfig(props_path=str(config_file.parent))
            client = TradeClient(cfg)
            positions = client.get_positions(sec_type=SecurityType.STK, market=Market.US) or []
            return pd.DataFrame([self._position_row(item) for item in positions]), None
        except ImportError:
            return None, "未安装 tigeropen SDK，请安装项目依赖"
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            return None, f"Tiger OpenAPI 持仓读取失败（{name}）；详细信息仅记录在服务端"

    @staticmethod
    def _position_row(item) -> dict:
        contract = getattr(item, "contract", None)
        symbol = str(getattr(contract, "symbol", "") or "").upper()
        return {
            "code": f"US.{symbol}",
            "stock_name": getattr(contract, "name", None) or symbol,
            "qty": getattr(item, "position_qty", None) or getattr(item, "quantity", 0),
            "can_sell_qty": getattr(item, "salable_qty", None),
            "cost_price": getattr(item, "average_cost", None),
            "nominal_price": getattr(item, "market_price", None),
            "market_val": getattr(item, "market_value", None),
            "pl_val": getattr(item, "unrealized_pnl", None),
            "pl_ratio": getattr(item, "unrealized_pnl_percent", None),
            "currency": getattr(contract, "currency", None) or "USD",
            "provider": "tiger",
            "read_only": True,
        }
