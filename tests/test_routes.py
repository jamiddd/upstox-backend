from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import anyio
from fastapi.testclient import TestClient

from app.api.dependencies import (
    get_oi_snapshot_store,
    get_signal_snapshot_store,
    get_token_store,
    get_tracked_instruments_store,
    get_upstox_service,
    get_usd_inr_service,
)
from app.core.config import Settings, get_settings
from app.main import app
from app.services import instrument_rules_service
from app.services.instrument_rules_service import _MasterCache
from app.services.main_screen_service import MainScreenService, _CACHE
from app.services import oi_analysis_service
from app.services.oi_snapshot_store import OiStrikeDiff, OiStrikesDiff, SnapshotNotFoundError
from app.services.search_screen_service import _SEARCH_CACHE
from app.services import underlying_signals_service


class FakeTokenStore:
    def __init__(self, *, token: Optional[str] = "upstox-token") -> None:
        self.token = token
        self.saved: Optional[dict[str, Any]] = None
        self.cleared = False

    def has_token(self) -> bool:
        return self.token is not None

    def save(self, token_payload: dict[str, Any]) -> None:
        self.saved = token_payload
        self.token = token_payload["access_token"]

    def load_access_token(self) -> str:
        if self.token is None:
            from app.core.exceptions import UpstoxAuthRequiredError

            raise UpstoxAuthRequiredError("Upstox login is required")
        return self.token

    def clear(self) -> None:
        self.cleared = True
        self.token = None


class FakeUpstoxService:
    def __init__(self) -> None:
        self.place_order_call_count = 0

    def build_login_url(self, *, state: Optional[str] = None) -> str:
        return f"https://upstox.test/login?state={state}"

    async def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        return {"access_token": f"token-for-{code}"}

    async def get_profile(self, access_token: str) -> dict[str, Any]:
        if access_token == "expired-token":
            from app.core.exceptions import UpstoxApiError

            raise UpstoxApiError(
                "Invalid token used to access api",
                status_code=401,
                upstox_code="UDAPI100050",
            )
        return {"status": "success", "data": {"user_name": "Test User"}}

    async def get_ltp(self, access_token: str, instrument_key: str) -> dict[str, Any]:
        return {"status": "success", "data": {"token": access_token, "key": instrument_key}}

    async def get_quotes(self, access_token: str, instrument_key: str) -> dict[str, Any]:
        quotes = {
            "NSE_INDEX|Nifty 50": {
                "instrument_token": "NSE_INDEX|Nifty 50",
                "last_price": 25050.0,
                "ohlc": {
                    "open": 24900.0,
                    "high": 25100.0,
                    "low": 24850.0,
                    "close": 25050.0,
                },
                # Neither this nor net_change drive previous_close any more -- that's fetched
                # directly via get_historical_candle's daily-candle close instead (see
                # MainScreenService._fetch_previous_close's doc comment for why). Left here as
                # otherwise-realistic quote data, unused by that calculation specifically.
                "net_change": 100.0,
            },
            "NSE_FO|111": {
                "instrument_token": "NSE_FO|111",
                "last_price": 125.0,
                "depth": {
                    "buy": [{"price": 124.5}],
                    "sell": [{"price": 125.5}],
                },
            },
            "NSE_FO|222": {
                "instrument_token": "NSE_FO|222",
                "last_price": 90.0,
                "depth": {
                    "buy": [{"price": 89.5}],
                    "sell": [{"price": 90.5}],
                },
            },
            "GLOBAL_INDEX|^GSPC": {
                "instrument_token": "GLOBAL_INDEX|^GSPC",
                "last_price": 5555.5,
                "ohlc": {"open": 5500.0, "high": 5560.0, "low": 5495.0, "close": 5555.5},
                "net_change": 15.5,
            },
            # Nifty's own futures contract (see search_instruments' fixture below) -- used for the
            # underlying-signals VWAP tests. LTP set above the flat 24902ish typical price of the
            # rising candle series get_historical_candle/get_intraday_candle return for any
            # instrument_key, so VWAP resolves to a well-defined "above" position.
            "NSE_FO|53216": {
                "instrument_token": "NSE_FO|53216",
                "last_price": 25050.0,
            },
        }
        return {
            "status": "success",
            "data": {
                key: quotes[key]
                for key in instrument_key.split(",")
                if key in quotes
            },
        }

    async def get_brokerage(
        self,
        access_token: str,
        *,
        instrument_key: str,
        quantity: int,
        product: str,
        transaction_type: str,
        price: float,
    ) -> dict[str, Any]:
        return {
            "status": "success",
            "data": {
                "charges": {
                    "total": 24.58,
                    "brokerage": 20.0,
                    "taxes": {"gst": 3.6, "stt": 0.75, "stamp_duty": 0.06},
                    "other_charges": {
                        "transaction": 0.12,
                        "clearing": 0.0,
                        "ipft": 0.03,
                        "sebi_turnover": 0.02,
                    },
                },
                "request": {
                    "access_token": access_token,
                    "instrument_key": instrument_key,
                    "quantity": quantity,
                    "product": product,
                    "transaction_type": transaction_type,
                    "price": price,
                },
            },
        }

    async def get_holdings(self, access_token: str) -> dict[str, Any]:
        return {"status": "success", "data": [{"token": access_token}]}

    async def get_positions(self, access_token: str) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "instrument_token": "NSE_FO|111",
                    "trading_symbol": "NIFTY26JUL25000CE",
                    "quantity": 75,
                    "average_price": 120.0,
                    "last_price": 125.0,
                    "pnl": 375.0,
                },
                {
                    "instrument_token": "NSE_FO|closed",
                    "trading_symbol": "NIFTY26JUL24000PE",
                    "quantity": 0,
                    "average_price": 80.0,
                    "last_price": 80.0,
                    "pnl": 25.0,
                },
            ],
        }

    async def get_order_book(self, access_token: str) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "order_id": "order-older",
                    "instrument_token": "NSE_FO|111",
                    "trading_symbol": "NIFTY26JUL25000CE",
                    "transaction_type": "BUY",
                    "order_type": "LIMIT",
                    "product": "I",
                    "status": "complete",
                    "quantity": 75,
                    "filled_quantity": 75,
                    "pending_quantity": 0,
                    "price": 120.0,
                    "average_price": 119.5,
                    "trigger_price": 0,
                    "order_timestamp": "2026-07-13 09:20:00",
                    "exchange_timestamp": "2026-07-13 09:20:01",
                    "status_message": "",
                },
                {
                    "order_id": "order-newer",
                    "instrument_token": "NSE_FO|222",
                    "trading_symbol": "NIFTY26JUL25000PE",
                    "transaction_type": "SELL",
                    "order_type": "MARKET",
                    "product": "I",
                    "status": "rejected",
                    "quantity": 75,
                    "filled_quantity": 0,
                    "pending_quantity": 0,
                    "price": 0,
                    "average_price": 0,
                    "trigger_price": 0,
                    "order_timestamp": "2026-07-13 09:25:00",
                    "exchange_timestamp": "",
                    "status_message": "Margin exceeded",
                },
            ],
        }

    async def get_historical_trades(
        self,
        access_token: str,
        *,
        segment: str,
        start_date: str,
        end_date: str,
        page_number: int,
        page_size: int,
    ) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "trade_id": "trade-older",
                    "instrument_token": "NSE_FO|111",
                    "symbol": "NIFTY26JUL25000CE",
                    "transaction_type": "BUY",
                    "quantity": 75,
                    "price": 120.0,
                    "amount": 9000.0,
                    "exchange": "NSE",
                    "segment": segment,
                    "option_type": "CE",
                    "strike_price": "25000",
                    "expiry": "2026-07-16",
                    "trade_date": "2026-07-10",
                },
                {
                    "trade_id": "trade-newer",
                    "instrument_token": "NSE_FO|222",
                    "symbol": "NIFTY26JUL25000PE",
                    "transaction_type": "SELL",
                    "quantity": 75,
                    "price": 90.0,
                    "amount": 6750.0,
                    "exchange": "NSE",
                    "segment": segment,
                    "option_type": "PE",
                    "strike_price": "25000",
                    "expiry": "2026-07-16",
                    "trade_date": "2026-07-12",
                },
            ],
            "meta_data": {
                "page": {
                    "page_number": page_number,
                    "page_size": page_size,
                    "total_records": 2,
                    "total_pages": 1,
                }
            },
        }

    async def place_gtt_order(
        self,
        access_token: str,
        order: dict[str, Any],
    ) -> dict[str, Any]:
        # Sentinel trigger price used by attach-exits tests to simulate one leg's placement
        # failing (the STOPLOSS rule specifically) without disturbing every other
        # place_gtt_order caller.
        if order["rules"][0]["strategy"] == "STOPLOSS" and order["rules"][0]["trigger_price"] == 105.0:
            from app.core.exceptions import UpstoxApiError

            raise UpstoxApiError(
                "GTT order cannot be placed",
                status_code=400,
                upstox_code="UDAPI100041",
            )
        return {
            "status": "success",
            "data": {
                "gtt_order_ids": ["GTT-123"],
            },
            "echo": order,
        }

    async def get_gtt_orders(self, access_token: str) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "gtt_order_id": "GTT-111",
                    "instrument_token": "NSE_FO|111",
                    "quantity": 75,
                    "product": "I",
                    "status": "ACTIVE",
                    "rules": [
                        {"strategy": "ENTRY", "trigger_type": "IMMEDIATE", "trigger_price": 125.5},
                        {"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": 140.0},
                        {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": 118.0},
                    ],
                },
                {
                    "gtt_order_id": "GTT-old",
                    "instrument_token": "NSE_FO|111",
                    "quantity": 75,
                    "product": "I",
                    "status": "CANCELLED",
                    "rules": [],
                },
                {
                    "gtt_order_id": "GTT-done",
                    "instrument_token": "NSE_FO|111",
                    "quantity": 75,
                    "product": "I",
                    "status": "COMPLETED",
                    "created_at": 1740641185000000,
                    "rules": [
                        {"strategy": "ENTRY", "trigger_type": "IMMEDIATE", "trigger_price": 100.0},
                        {"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": 115.0},
                        {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": 92.0},
                    ],
                },
                {
                    "gtt_order_id": "GTT-other",
                    "instrument_token": "NSE_FO|222",
                    "quantity": 75,
                    "product": "I",
                    "status": "ACTIVE",
                    "rules": [],
                },
            ],
        }

    async def modify_gtt_order(
        self,
        access_token: str,
        order: dict[str, Any],
    ) -> dict[str, Any]:
        if order["gtt_order_id"] == "GTT-fail":
            from app.core.exceptions import UpstoxApiError

            raise UpstoxApiError(
                "GTT order cannot be modified",
                status_code=400,
                upstox_code="UDAPI100041",
            )
        return {
            "status": "success",
            "data": {"gtt_order_id": order["gtt_order_id"]},
            "echo": order,
        }

    async def place_market_order(
        self,
        access_token: str,
        *,
        instrument_key: str,
        transaction_type: str,
        quantity: int,
        product: str,
    ) -> dict[str, Any]:
        return {
            "status": "success",
            "data": {"order_ids": [f"MKT-{instrument_key}"]},
        }

    async def place_order(
        self,
        access_token: str,
        *,
        instrument_key: str,
        transaction_type: str,
        quantity: int,
        product: str,
        order_type: str,
        price: float = 0,
        trigger_price: float = 0,
    ) -> dict[str, Any]:
        # Sentinel trigger price used by attach-exits tests to simulate one leg's placement
        # failing (the SL-M stoploss leg specifically) without disturbing every other place_order
        # caller.
        if order_type == "SL-M" and trigger_price == 105.0:
            from app.core.exceptions import UpstoxApiError

            raise UpstoxApiError(
                "Order cannot be placed",
                status_code=400,
                upstox_code="UDAPI100041",
            )
        self.place_order_call_count += 1
        return {
            "status": "success",
            "data": {"order_id": f"ORD-{order_type}-{self.place_order_call_count}"},
        }

    async def cancel_order(self, access_token: str, order_id: str) -> dict[str, Any]:
        return {"status": "success", "data": {"order_id": order_id}}

    async def modify_order(
        self,
        access_token: str,
        order: dict[str, Any],
    ) -> dict[str, Any]:
        if order["order_id"] == "order-fail":
            from app.core.exceptions import UpstoxApiError

            raise UpstoxApiError(
                "Order cannot be modified",
                status_code=400,
                upstox_code="UDAPI100041",
            )
        return {
            "status": "success",
            "data": {"order_id": order["order_id"]},
            "echo": order,
        }

    async def get_option_contracts(
        self,
        access_token: str,
        instrument_key: str,
        *,
        expiry_date: Optional[str] = None,
    ) -> dict[str, Any]:
        contracts = [
            {
                "name": "NIFTY",
                "expiry": "2026-07-16",
                "instrument_key": "NSE_FO|111",
                "trading_symbol": "NIFTY26JUL25000CE",
                "instrument_type": "CE",
                "underlying_symbol": "NIFTY",
                "strike_price": 25000,
                "lot_size": 65,
                "freeze_quantity": 1755.0,
                "tick_size": 5.0,
            },
            {
                "name": "NIFTY",
                "expiry": "2026-07-23",
                "instrument_key": "NSE_FO|222",
                "trading_symbol": "NIFTY26JUL25000PE",
                "instrument_type": "PE",
                "underlying_symbol": "NIFTY",
                "strike_price": 25000,
                "lot_size": 65,
                "freeze_quantity": 1755.0,
                "tick_size": 5.0,
            },
        ]
        if expiry_date:
            contracts = [contract for contract in contracts if contract["expiry"] == expiry_date]
        return {"status": "success", "data": contracts}

    async def get_option_chain(
        self,
        access_token: str,
        instrument_key: str,
        *,
        expiry_date: str,
    ) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "expiry": expiry_date,
                    "pcr": 0.92,
                    "strike_price": 25000.0,
                    "underlying_key": instrument_key,
                    "underlying_spot_price": 25050.0,
                    "call_options": {
                        "instrument_key": "NSE_FO|111",
                        "market_data": {
                            "ltp": 125.0,
                            "volume": 5400000.0,
                            "oi": 1250000.0,
                            "close_price": 118.0,
                            "bid_price": 124.5,
                            "bid_qty": 300.0,
                            "ask_price": 125.5,
                            "ask_qty": 450.0,
                            "prev_oi": 1180000.0,
                        },
                        "option_greeks": {
                            "vega": 12.1,
                            "theta": -18.4,
                            "gamma": 0.0012,
                            "delta": 0.52,
                            "iv": 14.2,
                            "pop": 48.0,
                        },
                    },
                    "put_options": {
                        "instrument_key": "NSE_FO|222",
                        "market_data": {
                            "ltp": 90.0,
                            "volume": 4100000.0,
                            "oi": 980000.0,
                            "close_price": 95.0,
                            "bid_price": 89.5,
                            "bid_qty": 200.0,
                            "ask_price": 90.5,
                            "ask_qty": 350.0,
                            "prev_oi": 1020000.0,
                        },
                        "option_greeks": {
                            "vega": 12.0,
                            "theta": -17.9,
                            "gamma": 0.0012,
                            "delta": -0.47,
                            "iv": 13.9,
                            "pop": 45.0,
                        },
                    },
                },
                {
                    "expiry": expiry_date,
                    "pcr": 0.88,
                    "strike_price": 25100.0,
                    "underlying_key": instrument_key,
                    "underlying_spot_price": 25050.0,
                    "call_options": {
                        "instrument_key": "NSE_FO|333",
                        "market_data": {
                            "ltp": 80.0,
                            "volume": 3000000.0,
                            "oi": 900000.0,
                            "close_price": 76.0,
                            "bid_price": 79.0,
                            "bid_qty": 150.0,
                            "ask_price": 81.5,
                            "ask_qty": 200.0,
                            "prev_oi": 870000.0,
                        },
                        "option_greeks": {
                            "vega": 11.0,
                            "theta": -16.0,
                            "gamma": 0.0011,
                            "delta": 0.38,
                            "iv": 14.0,
                            "pop": 40.0,
                        },
                    },
                    # No put_options here on purpose -- a deep strike with only one side listed
                    # (see option_chain()'s doc comment on this).
                },
            ],
        }

    async def get_oi(self, access_token, instrument_key, *, expiry, date):
        return {
            "status": "success",
            "data": {
                "expiry": "2026-07-23",
                "total_puts": 12500000,
                "total_calls": 9800000,
                "call_put_oi_data_list": [],
            },
        }

    async def get_change_oi(self, access_token, instrument_key, *, expiry, date, interval):
        return {"status": "success", "data": {"total_put_change_oi": 2500000}}

    async def get_max_pain(self, access_token, instrument_key, *, expiry, date, bucket_interval):
        return {"status": "success", "data": {"max_pain": 25000.0, "insights": []}}

    async def get_pcr(self, access_token, instrument_key, *, expiry, date, bucket_interval):
        return {"status": "success", "data": {"pcr": 1.2755, "insights": []}}

    async def get_historical_candle(
        self,
        access_token: str,
        instrument_key: str,
        *,
        unit: str,
        interval: str,
        to_date: str,
        from_date: Optional[str] = None,
    ) -> dict[str, Any]:
        if unit == "days":
            return {
                "status": "success",
                "data": {"candles": [["2026-07-17T00:00:00+05:30", 24900.0, 25100.0, 24850.0, 25050.0, 500000]]},
            }
        # A short but EMA/ATR-warm-up-sized rising 5m/15m series -- enough bars for both a
        # 9-period EMA and a 14-period ATR to produce a real (non-None) value. A constant 10-point
        # high/low spread and a steady +1/candle close both keep the true range flat at exactly
        # 10, so ATR(14) converges to a clean, hand-verifiable 10.0.
        start = datetime(2026, 7, 16, 9, 15)
        candles = [
            [
                (start + timedelta(minutes=5 * i)).isoformat() + "+05:30",
                24900.0 + i,
                24905.0 + i,
                24895.0 + i,
                24902.0 + i,
                1000,
            ]
            for i in range(20)
        ]
        return {"status": "success", "data": {"candles": candles}}

    async def get_intraday_candle(
        self,
        access_token: str,
        instrument_key: str,
        *,
        unit: str,
        interval: str,
    ) -> dict[str, Any]:
        return {"status": "success", "data": {"candles": []}}

    async def get_funds_and_margin(self, access_token: str) -> dict[str, Any]:
        if access_token == "funds-unavailable-token":
            # Mirrors Upstox's real nightly maintenance-window error (UDAPI100072) -- see
            # main_screen_service.summary()'s doc comment for why this must not take down the
            # whole bootstrap call.
            from app.core.exceptions import UpstoxApiError

            # Message/upstox_code here match what UpstoxService._build_api_error now extracts
            # from this exact real error shape (see that function's own test coverage) --
            # FakeUpstoxService stands in for UpstoxService entirely, so it must simulate that
            # extraction's result, not the raw response shape.
            raise UpstoxApiError(
                "The Funds service is accessible from 5:30 AM to 12:00 AM IST daily. Please "
                "try again during these service hours.",
                status_code=423,
                upstox_code="UDAPI100072",
                details={
                    "status": "error",
                    "errors": [
                        {
                            "errorCode": "UDAPI100072",
                            "message": (
                                "The Funds service is accessible from 5:30 AM to 12:00 AM IST "
                                "daily. Please try again during these service hours."
                            ),
                        }
                    ],
                },
            )
        return {
            "status": "success",
            "data": {
                "available_to_trade": {
                    "total": 99980.0,
                    "cash_available_to_trade": {
                        "total": 91980.0,
                        "cash": {
                            "opening_balance": 100000.0,
                            "added_today": 2000.0,
                            "withdrawn_today": -100.0,
                        },
                        "margin_used": {
                            "total": 9920.0,
                        },
                    },
                    "pledge_available_to_trade": {
                        "total": 8000.0,
                        "margin_used": {
                            "total": 80.0,
                        },
                    },
                },
            },
        }

    async def get_market_feed_authorize(self, access_token: str) -> dict[str, Any]:
        return {
            "status": "success",
            "data": {
                "authorized_redirect_uri": "wss://feed.test/socket?code=one-time",
            },
        }

    async def search_instruments(
        self,
        access_token: str,
        *,
        query: str,
        exchanges: str = "NSE,BSE",
        segments: str = "FO",
        instrument_types: str = "CE,PE",
        expiry: str = "current_month",
        atm_offset: int = 0,
        page_number: int = 1,
        records: int = 30,
    ) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "name": "Nifty 50",
                    "exchange": "NSE",
                    "instrument_type": "CE",
                    "underlying_key": "NSE_INDEX|Nifty 50",
                    "underlying_type": "INDEX",
                    "underlying_symbol": "NIFTY",
                    "lot_size": 75,
                    "freeze_quantity": 1800.0,
                    "tick_size": 5.0,
                },
                {
                    "name": "Nifty 50",
                    "exchange": "NSE",
                    "instrument_type": "PE",
                    "underlying_key": "NSE_INDEX|Nifty 50",
                    "underlying_type": "INDEX",
                    "underlying_symbol": "NIFTY",
                    "lot_size": 75,
                    "freeze_quantity": 1800.0,
                    "tick_size": 5.0,
                },
                {
                    "name": "RELIANCE INDUSTRIES LTD",
                    "exchange": "NSE",
                    "instrument_type": "CE",
                    "underlying_key": "NSE_EQ|INE002A01018",
                    "underlying_type": "EQUITY",
                    "underlying_symbol": "RELIANCE",
                    "lot_size": 500,
                    "freeze_quantity": 10000.0,
                    "tick_size": 5.0,
                },
                {
                    "name": "Gold",
                    "exchange": "MCX",
                    "instrument_type": "CE",
                    "underlying_key": "MCX_FO|123",
                    "underlying_type": "COM",
                    "underlying_symbol": "GOLD",
                    "lot_size": 100,
                    "freeze_quantity": 10000.0,
                    "tick_size": 5.0,
                },
                {
                    "name": "Nifty Future",
                    "exchange": "NSE",
                    "instrument_type": "FUT",
                    "instrument_key": "NSE_FO|53216",
                    "trading_symbol": "NIFTY FUT 31 JUL 26",
                    "underlying_key": "NSE_INDEX|Nifty 50",
                    "underlying_type": "INDEX",
                    "underlying_symbol": "NIFTY",
                    "lot_size": 75,
                    "freeze_quantity": 1800.0,
                    "tick_size": 5.0,
                },
            ],
            "meta_data": {
                "page": {
                    "page_number": page_number,
                    "records": records,
                    "total_records": 5,
                    "total_pages": 1,
                }
            },
        }


def _settings() -> Settings:
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=Path("/tmp/token.enc"),
        pending_oco_pairs_path=Path("/tmp/pending_oco_pairs.json"),
    )


def _client(token_store: Optional[FakeTokenStore] = None) -> TestClient:
    _CACHE.clear()
    _SEARCH_CACHE.clear()
    oi_analysis_service._CACHE = {}
    underlying_signals_service._CACHE = {}
    underlying_signals_service._HISTORY = {}
    instrument_rules_service._CACHE = _MasterCache(
        expires_at=9999999999,
        by_key={
            "NSE_FO|111": {
                "instrument_key": "NSE_FO|111",
                "lot_size": 75,
                "freeze_quantity": 1800,
                "tick_size": 5.0,
                "trading_symbol": "NIFTY26JUL25000CE",
            }
        },
    )
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_upstox_service] = FakeUpstoxService
    app.dependency_overrides[get_token_store] = lambda: token_store or FakeTokenStore()
    # Existing route tests exercise the legacy pure in-memory delta helper. Durable-store behavior
    # and the history route have focused tests of their own.
    app.dependency_overrides[get_signal_snapshot_store] = lambda: None
    # Same reasoning -- /main/underlying-signals also depends on this now (for OI(S)/OI(R)'s own
    # delta lookup, see UnderlyingSignalsService._oi_analysis), and the real dependency tries to
    # create Settings.oi_database_path's parent directory, which doesn't exist in this sandbox.
    app.dependency_overrides[get_oi_snapshot_store] = lambda: None
    return TestClient(app)


def test_health_is_public() -> None:
    """The deployment health endpoint does not require app auth."""
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_auth_status_reports_stored_token() -> None:
    """Return whether an Upstox token is present AND still actually valid."""
    client = _client(FakeTokenStore(token="upstox-token"))
    try:
        response = client.get("/api/auth/status", headers={"X-API-Key": "mobile-secret"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"authenticated": True}


def test_auth_status_reports_expired_token_as_unauthenticated() -> None:
    """FIX: a stored token *file* can exist while Upstox itself has expired it overnight -- this
    must actually probe Upstox (via get_profile), not just check that a file is present.
    """
    client = _client(FakeTokenStore(token="expired-token"))
    try:
        response = client.get("/api/auth/status", headers={"X-API-Key": "mobile-secret"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


def test_auth_status_reports_no_token_as_unauthenticated() -> None:
    """No stored token at all -- should short-circuit without calling Upstox."""
    client = _client(FakeTokenStore(token=None))
    try:
        response = client.get("/api/auth/status", headers={"X-API-Key": "mobile-secret"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


def test_auth_callback_saves_token_and_redirects_to_the_mobile_app() -> None:
    """Exchange the auth code, save the token payload, and hand the in-app browser back to the
    app via its own custom-scheme URL -- see auth_callback's doc comment for why this replaced a
    bare JSON response (nothing used to tell the Chrome Custom Tab to close).

    follow_redirects=False is required here -- httpx's TestClient (which normally follows
    redirects automatically) doesn't recognize the "personalscalper://" scheme and raises trying
    to, since it's not a real network scheme it knows how to route.
    """
    token_store = FakeTokenStore(token=None)
    client = _client(token_store)
    try:
        response = client.get("/api/auth/callback?code=abc", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 307
    assert response.headers["location"] == "personalscalper://auth/callback?status=success"
    assert token_store.saved == {"access_token": "token-for-abc"}


def test_auth_callback_does_not_require_mobile_api_key() -> None:
    """Allow Upstox browser redirects to call the OAuth callback."""
    token_store = FakeTokenStore(token=None)
    client = _client(token_store)
    try:
        response = client.get("/api/auth/callback?code=redirect-code", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 307
    assert token_store.saved == {"access_token": "token-for-redirect-code"}


def test_market_route_uses_stored_token() -> None:
    """Proxy market data calls through the Upstox service."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/market/ltp?instrument_key=NSE_EQ%7CINE848E01016",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["data"] == {
        "token": "stored-token",
        "key": "NSE_EQ|INE848E01016",
    }


class _FakeUsdInrService:
    def __init__(self, quote: dict[str, float] | None) -> None:
        self._quote = quote

    async def get_quote(self) -> dict[str, float] | None:
        return self._quote


def test_market_usd_inr_returns_quote_on_success() -> None:
    """No Upstox token needed at all -- this route doesn't touch the user's Upstox account."""
    client = _client()
    app.dependency_overrides[get_usd_inr_service] = lambda: _FakeUsdInrService(
        {"ltp": 96.27, "previous_close": 96.335},
    )
    try:
        response = client.get("/api/market/usd-inr", headers={"X-API-Key": "mobile-secret"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ltp": 96.27, "previous_close": 96.335}


def test_market_usd_inr_degrades_to_null_fields_when_source_unavailable() -> None:
    """A failed/unparseable Yahoo fetch degrades to null fields, not an HTTP error -- this is a
    "nice to have" ticker entry, not core trading data."""
    client = _client()
    app.dependency_overrides[get_usd_inr_service] = lambda: _FakeUsdInrService(None)
    try:
        response = client.get("/api/market/usd-inr", headers={"X-API-Key": "mobile-secret"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ltp": None, "previous_close": None}


def test_upstox_backed_route_requires_token() -> None:
    """Upstox-backed routes return 401 until OAuth has completed."""
    client = _client(FakeTokenStore(token=None))
    try:
        response = client.get(
            "/api/portfolio/holdings",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert response.json() == {"status": "error", "message": "Upstox login is required"}


def test_main_bootstrap_returns_screen_ready_payload() -> None:
    """Return initial main-screen data in the backend contract shape."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/bootstrap",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["underlying"] == {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "symbol": "NIFTY",
        "name": "NIFTY",
        "spot_price": 25050.0,
        # From FakeUpstoxService.get_historical_candle's daily-candle fixture (its "close" field,
        # not a live quote's net_change -- see MainScreenService._fetch_previous_close).
        "previous_close": 25050.0,
    }
    assert payload["expiries"] == ["2026-07-16", "2026-07-23"]
    assert payload["selected_expiry"] == "2026-07-16"
    assert payload["summary"] == {
        "opening_balance": 100000.0,
        "profit_loss": 400.0,
        "closing_balance": 102300.0,
        "available_margin": 99980.0,
        "margin_used": 10000.0,
        "payin_amount": 1900.0,
        "funds_unavailable_note": None,
    }
    assert payload["open_positions"] == [
        {
            "instrument_key": "NSE_FO|111",
            "trading_symbol": "NIFTY26JUL25000CE",
            "quantity": 75.0,
            "entry_price": 120.0,
            "last_price": 125.0,
            "pnl": 375.0,
        }
    ]


def test_main_bootstrap_degrades_gracefully_when_funds_service_unavailable() -> None:
    """A funds/margin failure (e.g. Upstox's nightly maintenance window) must not take down the
    whole bootstrap call -- spot price, expiries, and positions are all independently available.
    """
    client = _client(FakeTokenStore(token="funds-unavailable-token"))
    try:
        response = client.get(
            "/api/main/bootstrap",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    # Unaffected -- these don't depend on the funds/margin call at all.
    assert payload["underlying"]["spot_price"] == 25050.0
    assert payload["expiries"] == ["2026-07-16", "2026-07-23"]
    assert payload["open_positions"] != []
    # Funds-derived fields degrade to 0 rather than the whole request failing, with a note
    # explaining why instead of silently looking like an empty/zero account.
    summary = payload["summary"]
    assert summary["opening_balance"] == 0.0
    assert summary["available_margin"] == 0.0
    assert summary["margin_used"] == 0.0
    assert summary["payin_amount"] == 0.0
    # profit_loss is unaffected since it comes from positions, not funds.
    assert summary["profit_loss"] == 400.0
    assert summary["funds_unavailable_note"] == (
        "The Funds service is accessible from 5:30 AM to 12:00 AM IST daily. Please try again "
        "during these service hours."
    )


class _GapDownFakeUpstoxService(FakeUpstoxService):
    """NIFTY's LTP (25050.0, from the shared get_quotes fixture) is *below* yesterday's actual
    close (25300.0, returned here) -- a gap-down day. Exists to prove previous_close reflects a
    real gap correctly (negative change), not just the coincidental case where every other test's
    daily-candle fixture happens to equal last_price (making change always 0.0 and hiding a wrong-
    direction bug like the one this fix addresses -- see MainScreenService._fetch_previous_close).
    """

    async def get_historical_candle(
        self,
        access_token: str,
        instrument_key: str,
        *,
        unit: str,
        interval: str,
        to_date: str,
        from_date: Optional[str] = None,
    ) -> dict[str, Any]:
        if unit == "days":
            return {
                "status": "success",
                "data": {"candles": [["2026-07-17T00:00:00+05:30", 25250.0, 25350.0, 25200.0, 25300.0, 500000]]},
            }
        return await super().get_historical_candle(
            access_token, instrument_key, unit=unit, interval=interval, to_date=to_date, from_date=from_date,
        )


def test_main_bootstrap_reflects_a_real_gap_down_correctly() -> None:
    client = _client(FakeTokenStore(token="stored-token"))
    app.dependency_overrides[get_upstox_service] = _GapDownFakeUpstoxService
    try:
        response = client.get(
            "/api/main/bootstrap",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    underlying = response.json()["underlying"]
    assert underlying["spot_price"] == 25050.0
    assert underlying["previous_close"] == 25300.0
    # The actual point of this fix: LTP below the real previous close must compute as a genuine
    # loss, never a false gain.
    change_percent = (underlying["spot_price"] - underlying["previous_close"]) / underlying["previous_close"] * 100.0
    assert change_percent < 0.0


def test_main_selected_quote_returns_bid_and_ask_for_selected_strike() -> None:
    """Resolve the app-selected strike into a contract and return button prices."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/selected-quote"
            "?expiry_date=2026-07-16&strike_price=25000&option_type=CE",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "underlying": {
            "instrument_key": "NSE_INDEX|Nifty 50",
            "spot_price": 25050.0,
        },
        "contract": {
            "instrument_key": "NSE_FO|111",
            "trading_symbol": "NIFTY26JUL25000CE",
            "strike_price": 25000.0,
            "option_type": "CE",
            "lot_size": 65.0,
            "freeze_quantity": 1755.0,
            "tick_size": 0.05,
            "ltp": 125.0,
            "bid_price": 124.5,
            "ask_price": 125.5,
        },
    }


def test_main_option_chain_returns_live_market_data_and_greeks_per_strike() -> None:
    """Every strike's CE/PE market_data + option_greeks, reshaped flat -- see option_chain()'s
    doc comment for why this replaced the old bare-contract-metadata shape (powers the app's
    smart strike selector).
    """
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/option-chain?expiry_date=2026-07-16&underlying_key=NSE_INDEX|Nifty 50",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "underlying_key": "NSE_INDEX|Nifty 50",
        "expiry_date": "2026-07-16",
        "underlying_spot_price": 25050.0,
        # From FakeUpstoxService.get_option_contracts (the same /option/contract lookup order
        # placement uses) -- not FakeUpstoxService's separate instrument-rules-cache fixture
        # (lot_size 75), confirming lot_size now comes from that single shared source. See
        # option_chain()/_lot_size's own doc comments for why those two must not be allowed to
        # silently disagree.
        "lot_size": 65,
        "strikes": [
            {
                "strike_price": 25000.0,
                "ce": {
                    "instrument_key": "NSE_FO|111",
                    "ltp": 125.0,
                    "bid_price": 124.5,
                    "ask_price": 125.5,
                    "bid_qty": 300.0,
                    "ask_qty": 450.0,
                    "oi": 1250000.0,
                    "prev_oi": 1180000.0,
                    "volume": 5400000.0,
                    "delta": 0.52,
                    "gamma": 0.0012,
                    "theta": -18.4,
                    "vega": 12.1,
                    "iv": 14.2,
                },
                "pe": {
                    "instrument_key": "NSE_FO|222",
                    "ltp": 90.0,
                    "bid_price": 89.5,
                    "ask_price": 90.5,
                    "bid_qty": 200.0,
                    "ask_qty": 350.0,
                    "oi": 980000.0,
                    "prev_oi": 1020000.0,
                    "volume": 4100000.0,
                    "delta": -0.47,
                    "gamma": 0.0012,
                    "theta": -17.9,
                    "vega": 12.0,
                    "iv": 13.9,
                },
            },
            {
                "strike_price": 25100.0,
                "ce": {
                    "instrument_key": "NSE_FO|333",
                    "ltp": 80.0,
                    "bid_price": 79.0,
                    "ask_price": 81.5,
                    "bid_qty": 150.0,
                    "ask_qty": 200.0,
                    "oi": 900000.0,
                    "prev_oi": 870000.0,
                    "volume": 3000000.0,
                    "delta": 0.38,
                    "gamma": 0.0011,
                    "theta": -16.0,
                    "vega": 11.0,
                    "iv": 14.0,
                },
                # A strike missing a listed PE contract simply omits that side -- see
                # FakeUpstoxService.get_option_chain's second row.
                "pe": None,
            },
        ],
    }


def test_market_oi_analysis_returns_all_four_analysis_sections() -> None:
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/market/oi-analysis"
            "?expiry=current_week&date=2026-07-17"
            "&instrument_key=NSE_INDEX%7CNifty%2050"
            "&change_interval=2&bucket_interval=30",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "expiry": "2026-07-23",
        "date": "2026-07-17",
        "change_interval": 2,
        "bucket_interval": 30,
        "oi": {
            "expiry": "2026-07-23",
            "total_puts": 12500000,
            "total_calls": 9800000,
            "call_put_oi_data_list": [],
        },
        "change_oi": {"total_put_change_oi": 2500000},
        "max_pain": {"max_pain": 25000.0, "insights": []},
        "pcr": {"pcr": 1.2755, "insights": []},
    }


def test_main_position_quotes_returns_ltp_for_requested_keys() -> None:
    """Return compact LTP data for open positions tracked in the app."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/position-quotes?instrument_keys=NSE_FO%7C111,NSE_FO%7C222",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    # previous_close comes from FakeUpstoxService.get_historical_candle's daily-candle fixture
    # (same for every instrument key in this fake) -- see
    # MainScreenService._fetch_previous_close's doc comment for why this isn't derived from the
    # quote's own net_change field any more.
    assert response.json() == {
        "positions": [
            {"instrument_key": "NSE_FO|111", "ltp": 125.0, "previous_close": 25050.0},
            {"instrument_key": "NSE_FO|222", "ltp": 90.0, "previous_close": 25050.0},
        ]
    }


def test_main_position_quotes_supports_global_instrument_keys() -> None:
    """The same generic quote call also works for Upstox's Global Instruments (e.g. S&P 500) --
    used to poll the toolbar's Global watchlist ticker, which has no WebSocket feed support.
    """
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/position-quotes?instrument_keys=GLOBAL_INDEX%7C%5EGSPC",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "positions": [
            {"instrument_key": "GLOBAL_INDEX|^GSPC", "ltp": 5555.5, "previous_close": 25050.0},
        ]
    }


def test_main_underlying_signals_returns_ema_atr_opening_range_and_nearest_level() -> None:
    """End-to-end wiring for the underlying "trade tips" route -- exact EMA/ATR/pivot math is
    covered by test_underlying_signals_service.py's focused unit tests; this just proves the
    route/service/UpstoxService plumbing produces the right shape from FakeUpstoxService's fixed
    fake data.
    """
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/underlying-signals?underlying_key=NSE_INDEX|Nifty 50",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()

    assert payload["ltp"] == 25050.0
    assert payload["ema9_5m"]["position"] == "above"
    assert payload["ema9_15m"]["position"] == "above"
    # A flat 10-point true range across the whole fake series -> ATR converges to exactly 10.
    assert payload["atr14_5m"] == 10.0
    assert payload["opening_range"] == {"window_minutes": 15, "high": 24907.0, "low": 24895.0, "position": "above"}
    assert payload["previous_day"] == {"high": 25100.0, "low": 24850.0, "close": 25050.0}
    assert payload["pivots"] == {"p": 25000.0, "r1": 25150.0, "s1": 24900.0, "r2": 25250.0, "s2": 24750.0}
    # Both fake contracts share strike_price 25000 -- only one unique strike, so there's no gap
    # to derive a round-number step from.
    assert payload["round_step"] == 0.0
    # LTP (25050.0) exactly matches the fake previous-day close -> that's the unambiguous nearest
    # level, 0% away.
    assert payload["nearest_level"] == {"label": "Prev Day Close", "value": 25050.0, "distance_percent": 0.0}
    # Every directional tag now spells out the absolute point distance from LTP too (see
    # UnderlyingSignalsService._build_tags) -- prefix checks for the EMA tags (exact distance
    # depends on the fake series' EMA math, already covered by the service's own unit tests) and
    # exact strings for the two hand-computable ones (opening range high/low and nearest_level's
    # value are both known constants above).
    # The 5m and 15m EMA reads are folded into a single line (both "above" here) -- see
    # UnderlyingSignalsService._build_tags's merge doc comment.
    assert any(tag.startswith("Above 5m EMA9 by ") and "(15m Above by " in tag for tag in payload["tags"])
    assert "ATR 10" in payload["tags"]
    assert "Above opening range by 143.00" in payload["tags"]
    assert "Near Prev Day Close by 0.00" in payload["tags"]
    # No expiry_date was passed -- OI analysis (PCR/max pain) is skipped entirely.
    assert payload["pcr"] is None
    assert payload["max_pain"] is None
    # No underlying_symbol was passed either -- VWAP futures resolution is skipped entirely
    # (backward-compat: an older client that doesn't send it still gets a valid response).
    assert payload["vwap"] is None
    # Today's session open (the fake series' first 5m candle) is 24900.0, LTP is 25050.0 -- 150
    # points away, nowhere near the fixed 15-point no-trade-zone tolerance.
    assert payload["today_open"] == 24900.0
    assert payload["no_trade_zone"] is False


def test_main_underlying_signals_history_is_available_without_upstox_token() -> None:
    class _HistoryStore:
        def list_snapshots(self, **kwargs):
            assert kwargs == {
                "underlying_key": "NSE_INDEX|Nifty 50",
                "expiry_date": "2026-07-23",
                "limit": 25,
            }
            return [
                {
                    "expiry_date": "2026-07-23",
                    "trading_date": "2026-07-21",
                    "slot_start": "2026-07-21T03:45:00+00:00",
                    "observed_at": "2026-07-21T03:45:08+00:00",
                    "atr": 20.5,
                    "pcr": 1.2,
                },
            ]

    client = _client(FakeTokenStore(token=None))
    app.dependency_overrides[get_signal_snapshot_store] = _HistoryStore
    try:
        response = client.get(
            "/api/main/underlying-signals/history"
            "?underlying_key=NSE_INDEX|Nifty 50&expiry_date=2026-07-23&limit=25",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "underlying_key": "NSE_INDEX|Nifty 50",
        "expiry_date": "2026-07-23",
        "snapshots": [
            {
                "expiry_date": "2026-07-23",
                "trading_date": "2026-07-21",
                "slot_start": "2026-07-21T03:45:00+00:00",
                "observed_at": "2026-07-21T03:45:08+00:00",
                "atr": 20.5,
                "pcr": 1.2,
            },
        ],
    }


def test_main_oi_snapshot_history_is_lightweight_and_available_without_upstox_token() -> None:
    class _OISnapshotStore:
        def list_snapshots(self, **kwargs):
            assert kwargs == {
                "underlying_key": "NSE_INDEX|Nifty 50",
                "expiry_date": "2026-07-23",
                "limit": 25,
            }
            return [
                {
                    "expiry_date": "2026-07-23",
                    "trading_date": "2026-07-23",
                    "slot_start": "2026-07-23T04:00:00+00:00",
                    "observed_at": "2026-07-23T04:00:04+00:00",
                    "total_call_oi": 48512300.0,
                    "total_put_oi": 39882100.0,
                    "pcr": 0.82,
                    "max_pain": 25000.0,
                },
            ]

    client = _client(FakeTokenStore(token=None))
    app.dependency_overrides[get_oi_snapshot_store] = _OISnapshotStore
    try:
        response = client.get(
            "/api/main/oi-snapshots/history"
            "?underlying_key=NSE_INDEX|Nifty 50&expiry_date=2026-07-23&limit=25",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "underlying_key": "NSE_INDEX|Nifty 50",
        "expiry_date": "2026-07-23",
        "snapshots": [
            {
                "trading_date": "2026-07-23",
                "slot_start": "2026-07-23T04:00:00+00:00",
                "observed_at": "2026-07-23T04:00:04+00:00",
                "total_call_oi": 48512300.0,
                "total_put_oi": 39882100.0,
                "pcr": 0.82,
                "max_pain": 25000.0,
            },
        ],
    }


def test_main_oi_snapshot_diff_returns_exact_slot_changes_without_upstox_token() -> None:
    class _OISnapshotStore:
        def diff_strikes(self, **kwargs):
            assert kwargs["underlying_key"] == "NSE_INDEX|Nifty 50"
            assert kwargs["expiry_date"] == "2026-07-23"
            assert kwargs["from_slot"] == datetime.fromisoformat("2026-07-23T09:30:00+00:00")
            assert kwargs["to_slot"] == datetime.fromisoformat("2026-07-23T10:15:00+00:00")
            return OiStrikesDiff(
                underlying_symbol="NIFTY",
                total_call_oi_change=1245000.0,
                total_put_oi_change=-382000.0,
                strikes=[OiStrikeDiff(25000.0, 412000.0, -95000.0, 4700000.0, 1650000.0)],
            )

    client = _client(FakeTokenStore(token=None))
    app.dependency_overrides[get_oi_snapshot_store] = _OISnapshotStore
    try:
        response = client.get(
            "/api/main/oi-snapshots/diff?underlying_key=NSE_INDEX|Nifty 50"
            "&expiry_date=2026-07-23&from_slot=2026-07-23T09:30:00%2B00:00"
            "&to_slot=2026-07-23T10:15:00%2B00:00",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "underlying_key": "NSE_INDEX|Nifty 50",
        "underlying_symbol": "NIFTY",
        "expiry_date": "2026-07-23",
        "from_slot": "2026-07-23T09:30:00+00:00",
        "to_slot": "2026-07-23T10:15:00+00:00",
        "total_call_oi_change": 1245000.0,
        "total_put_oi_change": -382000.0,
        "strikes": [
            {
                "strike_price": 25000.0,
                "call_oi_change": 412000.0,
                "put_oi_change": -95000.0,
                "call_oi": 4700000.0,
                "put_oi": 1650000.0,
            },
        ],
    }


def test_main_oi_snapshot_diff_rejects_non_increasing_slots() -> None:
    client = _client(FakeTokenStore(token=None))
    app.dependency_overrides[get_oi_snapshot_store] = lambda: object()
    try:
        response = client.get(
            "/api/main/oi-snapshots/diff?underlying_key=NSE_INDEX|Nifty 50"
            "&expiry_date=2026-07-23&from_slot=2026-07-23T10:15:00%2B00:00"
            "&to_slot=2026-07-23T09:30:00%2B00:00",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json() == {"status": "error", "message": "to_slot must be strictly after from_slot"}


def test_main_oi_snapshot_diff_names_the_missing_slot() -> None:
    missing = datetime.fromisoformat("2026-07-23T10:15:00+00:00")

    class _OISnapshotStore:
        def diff_strikes(self, **kwargs):
            raise SnapshotNotFoundError(slot=missing, which="to_slot")

    client = _client(FakeTokenStore(token=None))
    app.dependency_overrides[get_oi_snapshot_store] = _OISnapshotStore
    try:
        response = client.get(
            "/api/main/oi-snapshots/diff?underlying_key=NSE_INDEX|Nifty 50"
            "&expiry_date=2026-07-23&from_slot=2026-07-23T09:30:00%2B00:00"
            "&to_slot=2026-07-23T10:15:00%2B00:00",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {
        "status": "error",
        "message": "to_slot snapshot was not found for slot 2026-07-23T10:15:00+00:00",
    }


class _NearDayOpenFakeUpstoxService(FakeUpstoxService):
    """LTP set just 5 points above the shared candle series' first-candle open (24900.0) -- well
    within the dynamic no-trade-zone tolerance (the shared rising candle series' ATR(14) on 5m
    yields 10.0, scaled by _NO_TRADE_ZONE_ATR_MULTIPLIER=0.75 to a 7.5-point tolerance) -- to
    exercise the caution end to end."""

    async def get_quotes(self, access_token: str, instrument_key: str) -> dict[str, Any]:
        result = await super().get_quotes(access_token, instrument_key)
        if instrument_key == "NSE_INDEX|Nifty 50":
            result["data"]["NSE_INDEX|Nifty 50"]["last_price"] = 24905.0
        return result


def test_main_underlying_signals_flags_no_trade_zone_near_days_open() -> None:
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_upstox_service] = _NearDayOpenFakeUpstoxService
    app.dependency_overrides[get_token_store] = lambda: FakeTokenStore(token="stored-token")
    app.dependency_overrides[get_signal_snapshot_store] = lambda: None
    app.dependency_overrides[get_oi_snapshot_store] = lambda: None
    _CACHE.clear()
    _SEARCH_CACHE.clear()
    oi_analysis_service._CACHE = {}
    underlying_signals_service._CACHE = {}
    underlying_signals_service._HISTORY = {}
    client = TestClient(app)
    try:
        response = client.get(
            "/api/main/underlying-signals?underlying_key=NSE_INDEX|Nifty 50",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()

    assert payload["today_open"] == 24900.0
    assert payload["no_trade_zone"] is True
    assert payload["tags"][0] == "No-Trade Zone -- within 7.5 of Day Open (24900)"


def test_main_underlying_signals_includes_vwap_when_underlying_symbol_is_given() -> None:
    """Passing underlying_symbol resolves the underlying's own futures contract (see
    FakeUpstoxService.search_instruments' "Nifty Future" fixture row, matched by underlying_key)
    and computes VWAP from its candles -- LTP for that instrument_key is faked at 25050.0, above
    the flat ~24900s typical price of the shared rising candle series, so it reads "above".
    """
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/underlying-signals?underlying_key=NSE_INDEX|Nifty 50&underlying_symbol=NIFTY",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()

    assert payload["vwap"] is not None
    assert payload["vwap"]["position"] == "above"
    assert any(tag.startswith("Above VWAP by ") for tag in payload["tags"])


def test_main_underlying_signals_resolves_sensex_vwap_from_niftys_own_future() -> None:
    """SENSEX has no futures market on Upstox -- per explicit product decision, its VWAP always
    resolves against Nifty's own futures contract instead (see UnderlyingSignalsService._is_sensex).
    """
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/underlying-signals?underlying_key=BSE_INDEX|SENSEX&underlying_symbol=SENSEX",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()

    assert payload["vwap"] is not None


def test_main_underlying_signals_omits_vwap_when_underlying_has_no_futures_market() -> None:
    """RELIANCE (already in FakeUpstoxService.search_instruments' fixture) has no FUT row of its
    own -- VWAP gracefully stays null, everything else in the response is unaffected.
    """
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/underlying-signals?underlying_key=NSE_EQ|INE002A01018&underlying_symbol=RELIANCE",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()

    assert payload["vwap"] is None
    assert payload["ltp"] is not None


def test_main_underlying_signals_includes_pcr_and_max_pain_when_expiry_date_is_given() -> None:
    """Passing expiry_date pulls in max-pain via the existing OI Analysis fakes (get_max_pain,
    already on FakeUpstoxService from that route's own tests: max_pain=25000.0). PCR is no longer
    Upstox's own whole-chain value (get_pcr's pcr=1.2755 fake is unused here) -- it's computed
    locally from get_oi's per-strike call_put_oi_data_list, restricted to the 5 strikes on each
    side of ATM (see UnderlyingSignalsService._oi_analysis); that fake list is empty, so pcr comes
    out None here, same as oi_support/oi_resistance below.
    """
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/underlying-signals?underlying_key=NSE_INDEX|Nifty 50&expiry_date=2026-07-23",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()

    # LTP (25050.0) sits above max pain (25000.0) -> bearish pull, +50.00 away.
    assert payload["max_pain"] == {"value": 25000.0, "pull": "bearish"}
    assert "MP 25000 (+50.0)" in payload["tags"]
    # FakeUpstoxService.get_oi's call_put_oi_data_list is empty (shared with the OI Analysis
    # route's own exact-match test, so not changed here) -- no per-strike data means no PCR/OI
    # support/resistance to compute, not an error.
    assert payload["pcr"] is None
    assert payload["oi_support"] is None
    assert payload["oi_resistance"] is None


def test_main_summary_returns_balance_pnl_and_closing_balance() -> None:
    """Return the summary section payload."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/summary",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "opening_balance": 100000.0,
        "profit_loss": 400.0,
        "closing_balance": 102300.0,
        "available_margin": 99980.0,
        "margin_used": 10000.0,
        "payin_amount": 1900.0,
        "funds_unavailable_note": None,
    }


def test_get_funds_and_margin_returns_raw_upstox_payload() -> None:
    """Return the complete V3 funds-and-margin response without reshaping it."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/user/get-funds-and-margin",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "data": {
            "available_to_trade": {
                "total": 99980.0,
                "cash_available_to_trade": {
                    "total": 91980.0,
                    "cash": {
                        "opening_balance": 100000.0,
                        "added_today": 2000.0,
                        "withdrawn_today": -100.0,
                    },
                    "margin_used": {
                        "total": 9920.0,
                    },
                },
                "pledge_available_to_trade": {
                    "total": 8000.0,
                    "margin_used": {
                        "total": 80.0,
                    },
                },
            },
        },
    }


def test_get_brokerage_returns_upstox_charge_estimate() -> None:
    """Forward a valid order estimate request to Upstox's brokerage calculator."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/charges/brokerage?instrument_key=NSE_FO%7C111&quantity=75"
            "&product=I&transaction_type=BUY&price=125.5",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["data"]["charges"]["total"] == 24.58
    assert response.json()["data"]["request"] == {
        "access_token": "stored-token",
        "instrument_key": "NSE_FO|111",
        "quantity": 75,
        "product": "I",
        "transaction_type": "BUY",
        "price": 125.5,
    }


def test_get_brokerage_rejects_invalid_order_parameters() -> None:
    """Reject invalid brokerage requests before a call reaches Upstox."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/charges/brokerage?instrument_key=NSE_FO%7C111&quantity=0"
            "&product=I&transaction_type=BUY&price=0",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_market_feed_authorize_returns_one_time_websocket_url() -> None:
    """Return Upstox's one-time market feed WebSocket URL."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/market/feed/authorize",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "data": {
            "authorized_redirect_uri": "wss://feed.test/socket?code=one-time",
        },
    }


def test_search_underlyings_returns_only_option_capable_indices_and_stocks() -> None:
    """Search screen returns deduped index/equity underlyings with F&O options."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/search/underlyings?query=nifty&limit=10",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "query": "nifty",
        "results": [
            {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "symbol": "NIFTY",
                "name": "Nifty 50",
                "underlying_type": "INDEX",
                "exchange": "NSE",
                "lot_size": 75.0,
                "freeze_quantity": 1800.0,
                "tick_size": 0.05,
                "is_optionable": True,
            },
            {
                "instrument_key": "NSE_EQ|INE002A01018",
                "symbol": "RELIANCE",
                "name": "RELIANCE INDUSTRIES LTD",
                "underlying_type": "EQUITY",
                "exchange": "NSE",
                "lot_size": 500.0,
                "freeze_quantity": 10000.0,
                "tick_size": 0.05,
                "is_optionable": True,
            },
        ],
        "page": {
            "page_number": 1,
            "records": 10,
            "total_records": 5,
            "total_pages": 1,
        },
    }


def test_search_underlyings_include_futures_merges_futures_contract() -> None:
    """include_futures=true additionally matches a FUT contract as its own watchlist-only entry
    -- excluded by default (see test_search_underlyings_returns_only_option_capable_indices_and_stocks).
    """
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/search/underlyings?query=nifty&limit=10&include_futures=true",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert {
        "instrument_key": "NSE_FO|53216",
        "symbol": "NIFTY FUT 31 JUL 26",
        "name": "Nifty Future",
        "underlying_type": "FUTURES",
        "exchange": "NSE",
        "lot_size": 75.0,
        "freeze_quantity": 1800.0,
        "tick_size": 0.05,
        "is_optionable": False,
    } in body["results"]


def test_search_underlyings_empty_query_returns_default_option_indices() -> None:
    """Empty search returns known index underlyings that provide options."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/search/underlyings?limit=2",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "query": "",
        "results": [
            {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "symbol": "NIFTY",
                "name": "Nifty 50",
                "underlying_type": "INDEX",
                "exchange": "NSE",
                "lot_size": 65.0,
                "freeze_quantity": 1755.0,
                "tick_size": 0.05,
                "is_optionable": True,
            },
            {
                "instrument_key": "NSE_INDEX|Nifty Bank",
                "symbol": "BANKNIFTY",
                "name": "Nifty Bank",
                "underlying_type": "INDEX",
                "exchange": "NSE",
                "lot_size": 30.0,
                "freeze_quantity": 600.0,
                "tick_size": 0.05,
                "is_optionable": True,
            },
        ],
        "page": {
            "page_number": 1,
            "records": 2,
            "total_records": 5,
            "total_pages": 3,
        },
    }


def test_search_underlyings_query_matches_non_optionable_index() -> None:
    """India VIX has no listed options, so Upstox's F&O search can never return it -- see
    NON_OPTIONABLE_INDICES's doc comment. Confirms it's merged in by name/symbol match anyway.
    """
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/search/underlyings?query=vix&limit=10",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert {
        "instrument_key": "NSE_INDEX|India VIX",
        "symbol": "INDIA VIX",
        "name": "India VIX",
        "underlying_type": "INDEX",
        "exchange": "NSE",
        "lot_size": 0.0,
        "freeze_quantity": 0.0,
        "is_optionable": False,
        "tick_size": 0.0,
    } in body["results"]


def test_order_history_today_returns_categorized_current_day_orders() -> None:
    """Order history defaults to current-day order book grouped by status."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/orders/history?scope=today&page_size=10",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == "today"
    assert payload["source"] == "order_book"
    assert [order["id"] for order in payload["orders"]] == ["order-newer", "order-older"]
    assert [order["id"] for order in payload["categories"]["rejected"]] == ["order-newer"]
    assert [order["id"] for order in payload["categories"]["complete"]] == ["order-older"]
    assert payload["page"] == {
        "page_number": 1,
        "page_size": 10,
        "total_records": 2,
        "total_pages": 1,
    }


def test_order_history_all_returns_paginated_historical_trades() -> None:
    """All mode returns paginated historical executed trades newest first."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/orders/history"
            "?scope=all&page_number=1&page_size=50&start_date=2026-04-01&end_date=2026-07-13",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == "all"
    assert payload["source"] == "historical_trades"
    assert "past executed trades" in payload["availability_note"]
    assert [order["id"] for order in payload["orders"]] == ["trade-newer", "trade-older"]
    assert [order["id"] for order in payload["categories"]["complete"]] == [
        "trade-newer",
        "trade-older",
    ]
    assert payload["filters"] == {
        "segment": "FO",
        "start_date": "2026-04-01",
        "end_date": "2026-07-13",
    }
    assert payload["page"] == {
        "page_number": 1,
        "page_size": 50,
        "total_records": 2,
        "total_pages": 1,
    }


def test_place_smart_bracket_order_submits_multi_leg_gtt() -> None:
    """Place a bracket-like order with client-provided GTT prices."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/smart-bracket",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "transaction_type": "BUY",
                "quantity": 75,
                "product": "I",
                "entry_trigger_type": "IMMEDIATE",
                "entry_trigger_price": 125.5,
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 118.0,
                "market_protection": -1,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["source"] == "upstox_gtt"
    assert payload["total_quantity"] == 75
    assert payload["slice_quantity"] == 75
    assert payload["slice_count"] == 1
    assert payload["slices"][0]["submitted_order"] == {
        "type": "MULTIPLE",
        "quantity": 75,
        "product": "I",
        "rules": [
            {
                "strategy": "ENTRY",
                "trigger_type": "IMMEDIATE",
                "trigger_price": 125.5,
                "market_protection": -1,
            },
            {
                "strategy": "TARGET",
                "trigger_type": "IMMEDIATE",
                "trigger_price": 140.0,
                "market_protection": -1,
            },
            {
                "strategy": "STOPLOSS",
                "trigger_type": "IMMEDIATE",
                "trigger_price": 118.0,
                "market_protection": -1,
            },
        ],
        "instrument_token": "NSE_FO|111",
        "transaction_type": "BUY",
    }
    assert payload["slices"][0]["upstox_response"]["data"] == {"gtt_order_ids": ["GTT-123"]}


def test_get_gtt_orders_filters_by_instrument_and_active_status() -> None:
    """Only ACTIVE orders for the requested instrument come back -- CANCELLED and other
    instruments' orders are excluded (see FakeUpstoxService.get_gtt_orders's fixture)."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/orders/gtt",
            headers={"X-API-Key": "mobile-secret"},
            params={"instrument_key": "NSE_FO|111"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert [order["gtt_order_id"] for order in payload] == ["GTT-111"]


def test_get_gtt_orders_with_history_includes_completed_but_not_cancelled() -> None:
    """include_history=true also returns COMPLETED brackets (so a closed order's historical
    target/stoploss can be recovered), but still excludes CANCELLED/REJECTED -- those never
    actually fired so they aren't a real target/stoploss the position had."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/orders/gtt",
            headers={"X-API-Key": "mobile-secret"},
            params={"instrument_key": "NSE_FO|111", "include_history": "true"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert {order["gtt_order_id"] for order in payload} == {"GTT-111", "GTT-done"}


class _PendingExitsFakeUpstoxService(FakeUpstoxService):
    """A resting SL-M stoploss order for NSE_FO|111, still open -- the order-book shape
    get_pending_exit_orders needs to resolve a pending exit's actual stoploss trigger price
    (quantity/product/target come from the stored PendingExit itself, not a live order -- see
    that class's own doc comment)."""

    async def get_order_book(self, access_token: str) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "order_id": "SL-1",
                    "instrument_token": "NSE_FO|111",
                    "status": "open",
                    "quantity": 75,
                    "product": "I",
                    "order_type": "SL-M",
                    "price": 0,
                    "trigger_price": 115.0,
                },
            ],
        }


def test_get_pending_exit_orders_resolves_a_registered_pair(tmp_path: Path) -> None:
    """A pending exit registered in PendingOcoPairsStore comes back with its stored target price
    and its live stoploss trigger price/quantity/product read from the order book."""
    from app.services.pending_oco_pairs_store import PendingExit, PendingOcoPairsStore

    pairs_path = tmp_path / "pairs.json"
    settings = replace(_settings(), pending_oco_pairs_path=pairs_path)
    PendingOcoPairsStore(settings).add(
        PendingExit(
            stoploss_order_id="SL-1",
            instrument_key="NSE_FO|111",
            exit_transaction_type="SELL",
            quantity=75,
            product="I",
            target_trigger_price=140.0,
        ),
    )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_upstox_service] = _PendingExitsFakeUpstoxService
    app.dependency_overrides[get_token_store] = lambda: FakeTokenStore(token="stored-token")
    client = TestClient(app)
    try:
        response = client.get(
            "/api/orders/pending-exits",
            headers={"X-API-Key": "mobile-secret"},
            params={"instrument_key": "NSE_FO|111"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload == [
        {
            "stoploss_order_id": "SL-1",
            "quantity": 75,
            "product": "I",
            "target_trigger_price": 140.0,
            "stoploss_trigger_price": 115.0,
        }
    ]


def test_get_pending_exit_orders_excludes_a_pending_exit_with_a_terminal_stoploss(tmp_path: Path) -> None:
    """A pending exit whose stoploss already filled/cancelled/rejected is stale -- oco_watcher
    will drop it on its own next tick, so it shouldn't be offered to the app for editing in the
    meantime."""
    from app.services.pending_oco_pairs_store import PendingExit, PendingOcoPairsStore

    pairs_path = tmp_path / "pairs.json"
    settings = replace(_settings(), pending_oco_pairs_path=pairs_path)
    # order-newer is FakeUpstoxService's default fixture order -- "rejected", i.e. already
    # terminal.
    PendingOcoPairsStore(settings).add(
        PendingExit(
            stoploss_order_id="order-newer",
            instrument_key="NSE_FO|111",
            exit_transaction_type="SELL",
            quantity=75,
            product="I",
            target_trigger_price=140.0,
        ),
    )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_upstox_service] = FakeUpstoxService
    app.dependency_overrides[get_token_store] = lambda: FakeTokenStore(token="stored-token")
    client = TestClient(app)
    try:
        response = client.get(
            "/api/orders/pending-exits",
            headers={"X-API-Key": "mobile-secret"},
            params={"instrument_key": "NSE_FO|111"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == []


def test_get_pending_exit_orders_returns_empty_when_nothing_pending(tmp_path: Path) -> None:
    pairs_path = tmp_path / "pairs.json"
    settings = replace(_settings(), pending_oco_pairs_path=pairs_path)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_upstox_service] = FakeUpstoxService
    app.dependency_overrides[get_token_store] = lambda: FakeTokenStore(token="stored-token")
    client = TestClient(app)
    try:
        response = client.get(
            "/api/orders/pending-exits",
            headers={"X-API-Key": "mobile-secret"},
            params={"instrument_key": "NSE_FO|111"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == []


def test_update_pending_exit_target_price_repoints_the_stored_target(tmp_path: Path) -> None:
    from app.services.pending_oco_pairs_store import PendingExit, PendingOcoPairsStore

    pairs_path = tmp_path / "pairs.json"
    settings = replace(_settings(), pending_oco_pairs_path=pairs_path)
    PendingOcoPairsStore(settings).add(
        PendingExit(
            stoploss_order_id="SL-1",
            instrument_key="NSE_FO|111",
            exit_transaction_type="SELL",
            quantity=75,
            product="I",
            target_trigger_price=140.0,
        ),
    )

    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    try:
        response = client.put(
            "/api/orders/pending-exits/target-price",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "stoploss_order_id": "SL-1",
                "target_trigger_price": 150.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "success"}
    assert PendingOcoPairsStore(settings).load()[0].target_trigger_price == 150.0


def test_update_pending_exit_target_price_404s_when_not_found(tmp_path: Path) -> None:
    pairs_path = tmp_path / "pairs.json"
    settings = replace(_settings(), pending_oco_pairs_path=pairs_path)

    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    try:
        response = client.put(
            "/api/orders/pending-exits/target-price",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "stoploss_order_id": "does-not-exist",
                "target_trigger_price": 150.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_modify_gtt_order_resends_full_rule_set() -> None:
    """Modifying a GTT bracket rebuilds ENTRY/TARGET/STOPLOSS together, not a partial patch."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.put(
            "/api/orders/gtt/modify",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "gtt_order_id": "GTT-111",
                "instrument_key": "NSE_FO|111",
                "quantity": 75,
                "product": "I",
                "entry_trigger_type": "IMMEDIATE",
                "entry_trigger_price": 125.5,
                "target_trigger_price": 145.0,
                "stoploss_trigger_price": 115.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"] == {"gtt_order_id": "GTT-111"}
    assert payload["echo"] == {
        "gtt_order_id": "GTT-111",
        "type": "MULTIPLE",
        "quantity": 75,
        "product": "I",
        "rules": [
            {"strategy": "ENTRY", "trigger_type": "IMMEDIATE", "trigger_price": 125.5},
            {"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": 145.0},
            {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": 115.0},
        ],
    }


def test_modify_gtt_order_rejects_invalid_tick_size() -> None:
    """Target/stoploss prices are validated against the instrument's tick size, same as
    placing a new smart-bracket order."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.put(
            "/api/orders/gtt/modify",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "gtt_order_id": "GTT-111",
                "instrument_key": "NSE_FO|111",
                "quantity": 75,
                "product": "I",
                "entry_trigger_type": "IMMEDIATE",
                "entry_trigger_price": 125.5,
                "target_trigger_price": 145.03,
                "stoploss_trigger_price": 115.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_modify_gtt_order_surfaces_upstox_failure() -> None:
    """Upstox rejecting the modify (e.g. GTT already triggered/cancelled) surfaces as an error,
    not a silent success."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.put(
            "/api/orders/gtt/modify",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "gtt_order_id": "GTT-fail",
                "instrument_key": "NSE_FO|111",
                "quantity": 75,
                "product": "I",
                "entry_trigger_type": "IMMEDIATE",
                "entry_trigger_price": 125.5,
                "target_trigger_price": 145.0,
                "stoploss_trigger_price": 115.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_attach_gtt_exits_places_only_the_stoploss_as_a_live_order() -> None:
    """Attaching exits to a position with no existing bracket places a single plain (non-GTT)
    SL-M stoploss order -- not a second live SELL for the target, which Upstox rejects outright
    (it has nothing left to "cover" once the stoploss has reserved the full held quantity). See
    SmartOrderService.attach_gtt_exits's own doc comment. The response still carries a "target"
    sub-object (mirroring "stoploss") purely so the app's existing per-leg error rendering keeps
    working -- there's no separate live order behind it any more."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/gtt/attach-exits",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "quantity": 75,
                "product": "I",
                "exit_transaction_type": "SELL",
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 115.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["total_quantity"] == 75
    assert payload["slice_count"] == 1
    slice_0 = payload["slices"][0]
    assert slice_0["quantity"] == 75
    expected_order = {
        "quantity": 75,
        "product": "I",
        "order_type": "SL-M",
        "price": 0.0,
        "trigger_price": 115.0,
        "instrument_token": "NSE_FO|111",
        "transaction_type": "SELL",
    }
    assert slice_0["target"]["submitted_order"] == expected_order
    assert slice_0["stoploss"]["submitted_order"] == expected_order


def test_attach_gtt_exits_registers_a_pending_oco_pair_for_oco_watcher(tmp_path: Path) -> None:
    """Both legs placing successfully registers the pair in PendingOcoPairsStore -- see that
    store's own doc comment and oco_watcher.run_oco_watcher, which reconciles it once one leg
    fills."""
    from app.services.pending_oco_pairs_store import PendingOcoPairsStore

    pairs_path = tmp_path / "pairs.json"
    client = _client(FakeTokenStore(token="stored-token"))
    app.dependency_overrides[get_settings] = lambda: replace(_settings(), pending_oco_pairs_path=pairs_path)
    try:
        response = client.post(
            "/api/orders/gtt/attach-exits",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "quantity": 75,
                "product": "I",
                "exit_transaction_type": "SELL",
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 115.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    pending_pairs = PendingOcoPairsStore(replace(_settings(), pending_oco_pairs_path=pairs_path)).load()
    assert len(pending_pairs) == 1
    assert pending_pairs[0].instrument_key == "NSE_FO|111"


def test_attach_gtt_exits_reports_error_when_the_stoploss_placement_fails() -> None:
    """The stoploss is the only live order placed now -- if it fails, there's nothing to fall
    back on (no separate target order was ever attempted). See FakeUpstoxService.place_order's
    SL-M-at-105.0 sentinel."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/gtt/attach-exits",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "quantity": 75,
                "product": "I",
                "exit_transaction_type": "SELL",
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 105.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "error"
    slice_0 = payload["slices"][0]
    assert slice_0["target"]["status"] == "error"
    assert slice_0["stoploss"]["status"] == "error"


def test_attach_gtt_exits_slices_large_quantity() -> None:
    """A quantity over the instrument's freeze quantity (1800 for NSE_FO|111) is sliced into
    multiple target/stoploss order pairs, same as smart-bracket's own entry slicing."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/gtt/attach-exits",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "quantity": 3750,
                "product": "I",
                "exit_transaction_type": "SELL",
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 115.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["total_quantity"] == 3750
    assert payload["slice_quantity"] == 1800
    assert payload["slice_count"] == 3
    assert [item["quantity"] for item in payload["slices"]] == [1800, 1800, 150]
    for item in payload["slices"]:
        assert item["target"]["status"] == "success"
        assert item["stoploss"]["status"] == "success"


def test_attach_gtt_exits_rejects_invalid_tick_size() -> None:
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/gtt/attach-exits",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "quantity": 75,
                "product": "I",
                "exit_transaction_type": "SELL",
                "target_trigger_price": 140.03,
                "stoploss_trigger_price": 115.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_place_market_bracket_order_enters_at_market_then_attaches_exits() -> None:
    """The entry is a real MARKET order (FakeUpstoxService.place_market_order), not a GTT ENTRY
    rule -- then target/stoploss GTT SINGLE legs are attached for the entered quantity, same as
    attach-exits."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/market-bracket",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "transaction_type": "BUY",
                "quantity": 75,
                "product": "I",
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 115.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["source"] == "upstox_market_with_gtt_exits"
    assert payload["total_quantity"] == 75
    assert payload["entered_quantity"] == 75
    assert payload["entry_slices"][0]["status"] == "success"
    assert payload["entry_slices"][0]["upstox_response"]["data"] == {"order_ids": ["MKT-NSE_FO|111"]}
    # Exit legs must flatten the opposite side of a BUY entry.
    assert payload["exits"]["status"] == "success"
    assert payload["exits"]["slices"][0]["target"]["submitted_order"]["transaction_type"] == "SELL"
    assert payload["exits"]["slices"][0]["stoploss"]["submitted_order"]["transaction_type"] == "SELL"


def test_place_market_bracket_order_slices_large_quantity() -> None:
    """A quantity over the instrument's freeze quantity is sliced into multiple market entry
    orders, same as smart-bracket's own entry slicing."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/market-bracket",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "transaction_type": "BUY",
                "quantity": 3750,
                "product": "I",
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 115.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["slice_quantity"] == 1800
    assert payload["slice_count"] == 3
    assert [item["quantity"] for item in payload["entry_slices"]] == [1800, 1800, 150]
    assert payload["entered_quantity"] == 3750
    assert payload["exits"]["slice_count"] == 3


def test_place_market_bracket_order_rejects_invalid_tick_size() -> None:
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/market-bracket",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "transaction_type": "BUY",
                "quantity": 75,
                "product": "I",
                "target_trigger_price": 140.03,
                "stoploss_trigger_price": 115.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_place_smart_bracket_order_slices_large_quantity() -> None:
    """Split large quantities so the client does not handle freeze slicing."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/smart-bracket",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "transaction_type": "BUY",
                "quantity": 3750,
                "product": "I",
                "entry_trigger_type": "IMMEDIATE",
                "entry_trigger_price": 125.5,
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 118.0,
                "slice_quantity": 1800,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_quantity"] == 3750
    assert payload["slice_quantity"] == 1800
    assert payload["slice_count"] == 3
    assert [item["quantity"] for item in payload["slices"]] == [1800, 1800, 150]
    assert [item["submitted_order"]["quantity"] for item in payload["slices"]] == [
        1800,
        1800,
        150,
    ]


def _set_exit_positions_instrument_rules_cache() -> None:
    """exit_positions/exit_all_positions now look up freeze quantity per position -- these tests
    build their own TestClient manually (not via _client()) so they need their own instrument
    rules cache seeded, covering both instruments _ExitAllFakeUpstoxService's fixture returns.
    NSE_FO|222's freeze quantity (5000) is well above its 150-quantity position so it never slices,
    keeping these tests' existing single-order assertions valid.
    """
    instrument_rules_service._CACHE = _MasterCache(
        expires_at=9999999999,
        by_key={
            "NSE_FO|111": {
                "instrument_key": "NSE_FO|111",
                "lot_size": 75,
                "freeze_quantity": 1800,
                "tick_size": 5.0,
                "trading_symbol": "NIFTY26JUL25000CE",
            },
            "NSE_FO|222": {
                "instrument_key": "NSE_FO|222",
                "lot_size": 150,
                "freeze_quantity": 5000,
                "tick_size": 5.0,
                "trading_symbol": "NIFTY26JUL25000PE",
            },
        },
    )


class _ExitAllFakeUpstoxService(FakeUpstoxService):
    """Two open positions (one long, one short) plus one already-closed one that must be
    skipped, and one instrument whose market order deliberately fails -- to verify exit-all is
    correctly signed per side and doesn't stop after one failure."""

    async def get_positions(self, access_token: str) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "instrument_token": "NSE_FO|111",
                    "trading_symbol": "NIFTY26JUL25000CE",
                    "quantity": 75,
                    "product": "I",
                    "average_price": 120.0,
                    "last_price": 125.0,
                    "pnl": 375.0,
                },
                {
                    "instrument_token": "NSE_FO|222",
                    "trading_symbol": "NIFTY26JUL25000PE",
                    "quantity": -150,
                    "product": "I",
                    "average_price": 90.0,
                    "last_price": 85.0,
                    "pnl": 750.0,
                },
                {
                    "instrument_token": "NSE_FO|closed",
                    "trading_symbol": "NIFTY26JUL24000PE",
                    "quantity": 0,
                    "product": "I",
                    "average_price": 80.0,
                    "last_price": 80.0,
                    "pnl": 25.0,
                },
            ],
        }

    async def place_market_order(
        self,
        access_token: str,
        *,
        instrument_key: str,
        transaction_type: str,
        quantity: int,
        product: str,
    ) -> dict[str, Any]:
        if instrument_key == "NSE_FO|222":
            from app.core.exceptions import UpstoxApiError

            raise UpstoxApiError("Order rejected", status_code=400, upstox_code="UDAPI100041")
        return {"status": "success", "data": {"order_ids": [f"MKT-{instrument_key}"]}}


def test_exit_all_positions_flattens_every_open_position() -> None:
    """SELLs a long, BUYs a short (opposite-signed exit), skips the already-closed position, and
    keeps going after one instrument's exit order fails."""
    _set_exit_positions_instrument_rules_cache()
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_upstox_service] = _ExitAllFakeUpstoxService
    app.dependency_overrides[get_token_store] = lambda: FakeTokenStore(token="stored-token")
    client = TestClient(app)
    try:
        response = client.post("/api/orders/exit-all", headers={"X-API-Key": "mobile-secret"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["positions_found"] == 2
    results_by_key = {item["instrument_key"]: item for item in payload["results"]}
    assert results_by_key["NSE_FO|111"]["transaction_type"] == "SELL"
    assert results_by_key["NSE_FO|111"]["quantity"] == 75
    assert results_by_key["NSE_FO|111"]["status"] == "success"
    assert results_by_key["NSE_FO|222"]["transaction_type"] == "BUY"
    assert results_by_key["NSE_FO|222"]["quantity"] == 150
    assert results_by_key["NSE_FO|222"]["status"] == "error"


def test_exit_positions_closes_every_open_position_when_unfiltered() -> None:
    """Omitting instrument_keys behaves identically to /orders/exit-all."""
    _set_exit_positions_instrument_rules_cache()
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_upstox_service] = _ExitAllFakeUpstoxService
    app.dependency_overrides[get_token_store] = lambda: FakeTokenStore(token="stored-token")
    client = TestClient(app)
    try:
        response = client.post(
            "/api/orders/exit-positions",
            headers={"X-API-Key": "mobile-secret"},
            json={},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["positions_found"] == 2
    results_by_key = {item["instrument_key"]: item for item in payload["results"]}
    assert set(results_by_key) == {"NSE_FO|111", "NSE_FO|222"}


def test_exit_positions_closes_only_the_requested_subset() -> None:
    """A given instrument_keys list scopes exit-positions to just those positions -- backs the
    app's "close only positive/negative positions" menu, which computes the subset client-side."""
    _set_exit_positions_instrument_rules_cache()
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_upstox_service] = _ExitAllFakeUpstoxService
    app.dependency_overrides[get_token_store] = lambda: FakeTokenStore(token="stored-token")
    client = TestClient(app)
    try:
        response = client.post(
            "/api/orders/exit-positions",
            headers={"X-API-Key": "mobile-secret"},
            json={"instrument_keys": ["NSE_FO|111"]},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["positions_found"] == 1
    assert payload["results"][0]["instrument_key"] == "NSE_FO|111"
    assert payload["results"][0]["status"] == "success"


class _LargePositionFakeUpstoxService(FakeUpstoxService):
    """One position sized over NSE_FO|111's freeze quantity (1800) -- to verify exit_positions
    slices its flattening order instead of submitting a single oversized one."""

    def __init__(self) -> None:
        self.place_market_order_calls: list[dict[str, Any]] = []
        self.fail_on_call_number: Optional[int] = None

    async def get_positions(self, access_token: str) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "instrument_token": "NSE_FO|111",
                    "trading_symbol": "NIFTY26JUL25000CE",
                    "quantity": 3750,
                    "product": "I",
                    "average_price": 120.0,
                    "last_price": 125.0,
                    "pnl": 375.0,
                },
            ],
        }

    async def place_market_order(
        self,
        access_token: str,
        *,
        instrument_key: str,
        transaction_type: str,
        quantity: int,
        product: str,
    ) -> dict[str, Any]:
        self.place_market_order_calls.append({"instrument_key": instrument_key, "quantity": quantity})
        if self.fail_on_call_number == len(self.place_market_order_calls):
            from app.core.exceptions import UpstoxApiError

            raise UpstoxApiError("Order rejected", status_code=400, upstox_code="UDAPI100041")
        return {"status": "success", "data": {"order_ids": [f"MKT-{instrument_key}"]}}


def test_exit_positions_slices_a_position_over_freeze_quantity() -> None:
    """A 3750-quantity position on NSE_FO|111 (freeze quantity 1800) is flattened via three
    separate market orders (1800/1800/150), not one oversized one Upstox would reject."""
    _set_exit_positions_instrument_rules_cache()
    fake_service = _LargePositionFakeUpstoxService()
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_upstox_service] = lambda: fake_service
    app.dependency_overrides[get_token_store] = lambda: FakeTokenStore(token="stored-token")
    client = TestClient(app)
    try:
        response = client.post("/api/orders/exit-all", headers={"X-API-Key": "mobile-secret"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["status"] == "success"
    assert payload["results"][0]["quantity"] == 3750
    assert [call["quantity"] for call in fake_service.place_market_order_calls] == [1800, 1800, 150]


def test_exit_positions_reports_error_when_a_slice_fails_partway() -> None:
    """A slice failing partway through a position's flatten is reported as that position's own
    error -- a half-flattened position isn't actually safe, so it must not read as "success"."""
    _set_exit_positions_instrument_rules_cache()
    fake_service = _LargePositionFakeUpstoxService()
    fake_service.fail_on_call_number = 2
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_upstox_service] = lambda: fake_service
    app.dependency_overrides[get_token_store] = lambda: FakeTokenStore(token="stored-token")
    client = TestClient(app)
    try:
        response = client.post("/api/orders/exit-all", headers={"X-API-Key": "mobile-secret"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["status"] == "error"
    # The first slice was already submitted before the second one failed.
    assert len(fake_service.place_market_order_calls) == 2


def test_modify_orders_accepts_more_than_upstream_multi_order_limit() -> None:
    """Process every order without imposing a bulk request count limit."""
    client = _client(FakeTokenStore(token="stored-token"))
    orders = [
        {
            "order_id": f"order-{index}",
            "validity": "DAY",
            "price": 125.0 + index,
            "order_type": "LIMIT",
            "trigger_price": 0,
            "quantity": 75,
        }
        for index in range(25)
    ]
    try:
        response = client.put(
            "/api/orders/modify",
            headers={"X-API-Key": "mobile-secret"},
            json={"orders": orders},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["summary"] == {"total": 25, "success": 25, "failed": 0}
    assert [order["order_id"] for order in payload["orders"]] == [
        f"order-{index}" for index in range(25)
    ]
    assert payload["orders"][-1]["upstox_response"]["echo"] == orders[-1]


def test_modify_orders_continues_after_an_individual_failure() -> None:
    """Return partial results and still attempt orders after a rejected one."""
    client = _client(FakeTokenStore(token="stored-token"))
    orders = [
        {
            "order_id": order_id,
            "validity": "DAY",
            "price": 125.0,
            "order_type": "LIMIT",
            "trigger_price": 0,
        }
        for order_id in ("order-1", "order-fail", "order-3")
    ]
    try:
        response = client.put(
            "/api/orders/modify",
            headers={"X-API-Key": "mobile-secret"},
            json={"orders": orders},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "partial_success"
    assert payload["summary"] == {"total": 3, "success": 2, "failed": 1}
    assert [order["status"] for order in payload["orders"]] == [
        "success",
        "error",
        "success",
    ]
    assert payload["orders"][1]["error"]["upstox_code"] == "UDAPI100041"


def test_place_smart_bracket_order_rejects_invalid_lot_size() -> None:
    """Reject quantities that are not a multiple of the instrument lot size."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/smart-bracket",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "transaction_type": "BUY",
                "quantity": 76,
                "product": "I",
                "entry_trigger_type": "IMMEDIATE",
                "entry_trigger_price": 125.5,
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 118.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json() == {
        "status": "error",
        "message": "Quantity 76 must be a multiple of lot size 75",
    }


def test_place_smart_bracket_order_rejects_invalid_tick_size() -> None:
    """Reject prices that are not aligned to the instrument tick size."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/smart-bracket",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "transaction_type": "BUY",
                "quantity": 75,
                "product": "I",
                "entry_trigger_type": "IMMEDIATE",
                "entry_trigger_price": 125.53,
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 118.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json() == {
        "status": "error",
        "message": "entry_trigger_price 125.53 must be a multiple of tick size 0.05",
    }


def test_resolve_underlying_symbol_and_expiry_picks_the_nearest_listed_expiry() -> None:
    """Used by the tracked-instruments background poller (see
    app.services.tracked_instruments_poller) to resolve what bootstrap would otherwise resolve
    from a live client request -- same underlying_symbol ("NIFTY", from FakeUpstoxService's own
    get_option_contracts fixture) and same nearest-expiry convention (expiries[0] once sorted --
    the fixture has 2026-07-16 and 2026-07-23, so the earlier one wins).
    """
    _CACHE.clear()
    service = MainScreenService(FakeUpstoxService())

    symbol, expiry = anyio.run(
        lambda: service.resolve_underlying_symbol_and_expiry("upstox-token", "NSE_INDEX|Nifty 50"),
    )

    assert symbol == "NIFTY"
    assert expiry == "2026-07-16"


def test_get_tracked_instruments_returns_an_empty_list_when_nothing_saved(tmp_path: Path) -> None:
    from app.services.tracked_instruments_store import TrackedInstrumentsStore

    store = TrackedInstrumentsStore(_settings())
    store.path = tmp_path / "tracked.json"
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_tracked_instruments_store] = lambda: store
    client = TestClient(app)
    try:
        response = client.get(
            "/api/user/tracked-instruments",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"underlying_keys": []}


def test_put_tracked_instruments_persists_and_echoes_the_new_set(tmp_path: Path) -> None:
    from app.services.tracked_instruments_store import TrackedInstrumentsStore

    store = TrackedInstrumentsStore(_settings())
    store.path = tmp_path / "tracked.json"
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_tracked_instruments_store] = lambda: store
    client = TestClient(app)
    try:
        response = client.put(
            "/api/user/tracked-instruments",
            headers={"X-API-Key": "mobile-secret"},
            json={"underlying_keys": ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"]},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"underlying_keys": ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"]}
    assert store.load() == ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"]


def test_put_tracked_instruments_replaces_the_previous_set(tmp_path: Path) -> None:
    from app.services.tracked_instruments_store import TrackedInstrumentsStore

    store = TrackedInstrumentsStore(_settings())
    store.path = tmp_path / "tracked.json"
    store.save(["NSE_INDEX|Nifty 50"])
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_tracked_instruments_store] = lambda: store
    client = TestClient(app)
    try:
        response = client.put(
            "/api/user/tracked-instruments",
            headers={"X-API-Key": "mobile-secret"},
            json={"underlying_keys": ["BSE_INDEX|SENSEX"]},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"underlying_keys": ["BSE_INDEX|SENSEX"]}


def test_tracked_instruments_endpoints_require_the_mobile_api_key(tmp_path: Path) -> None:
    from app.services.tracked_instruments_store import TrackedInstrumentsStore

    store = TrackedInstrumentsStore(_settings())
    store.path = tmp_path / "tracked.json"
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_tracked_instruments_store] = lambda: store
    client = TestClient(app)
    try:
        response = client.get("/api/user/tracked-instruments")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
