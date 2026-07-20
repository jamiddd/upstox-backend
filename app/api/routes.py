from __future__ import annotations

from datetime import date
import logging
from typing import Any, Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.api.dependencies import (
    get_token_store,
    get_tracked_instruments_store,
    get_upstox_service,
    get_usd_inr_service,
)
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AppConfigError,
    TokenStoreError,
    TrackedInstrumentsStoreError,
    UpstoxApiError,
    UpstoxAuthRequiredError,
)
from app.services.token_store import EncryptedTokenStore
from app.services.tracked_instruments_store import TrackedInstrumentsStore
from app.services.upstox_service import UpstoxService
from app.core.security import require_mobile_api_key
from app.services.instrument_rules_service import (
    InstrumentRulesService,
    slice_quantity_for_freeze,
    validate_price,
    validate_quantity,
)
from app.services.main_screen_service import DEFAULT_UNDERLYING_KEY, MainScreenService
from app.services.order_history_service import OrderHistoryService
from app.services.order_modification_service import OrderModificationService
from app.services.oi_analysis_service import OIAnalysisService
from app.services.search_screen_service import SearchScreenService
from app.services.smart_order_service import SmartOrderService
from app.services.underlying_signals_service import UnderlyingSignalsService
from app.services.usd_inr_service import UsdInrService

public_router = APIRouter()
protected_router = APIRouter(dependencies=[Depends(require_mobile_api_key)])

logger = logging.getLogger(__name__)


class SmartBracketOrderRequest(BaseModel):
    """Client-provided bracket-like GTT order parameters."""

    instrument_key: str = Field(min_length=1)
    transaction_type: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    product: Literal["I", "D", "MTF"] = "I"
    entry_trigger_type: Literal["ABOVE", "BELOW", "IMMEDIATE"] = "IMMEDIATE"
    entry_trigger_price: float = Field(gt=0)
    target_trigger_price: float = Field(gt=0)
    stoploss_trigger_price: float = Field(gt=0)
    trailing_gap: Optional[float] = Field(default=None, gt=0)
    market_protection: Optional[int] = Field(default=None, ge=-1, le=25)
    slice_quantity: Optional[int] = Field(default=None, gt=0)


class ModifyGttOrderRequest(BaseModel):
    """Re-points an existing GTT bracket's target/stoploss trigger prices. The entry fields are
    resent unchanged by the client (it already has them from GET /orders/gtt) -- Upstox's GTT
    modify contract expects the full rule set, not a partial patch.
    """

    gtt_order_id: str = Field(min_length=1)
    instrument_key: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    product: Literal["I", "D", "MTF"] = "I"
    entry_trigger_type: Literal["ABOVE", "BELOW", "IMMEDIATE"] = "IMMEDIATE"
    entry_trigger_price: float = Field(gt=0)
    target_trigger_price: float = Field(gt=0)
    stoploss_trigger_price: float = Field(gt=0)
    trailing_gap: Optional[float] = Field(default=None, gt=0)


class TrackedInstrumentsRequest(BaseModel):
    """Replaces the whole persisted set of underlying_keys the background poller keeps
    5-minute-change history warm for -- see TrackedInstrumentsStore. Always the client's full
    current Settings selection, not an incremental add/remove.
    """

    underlying_keys: list[str] = Field(default_factory=list)


class ExitPositionsRequest(BaseModel):
    """Optionally scopes /orders/exit-positions to a subset of open positions. None
    (instrument_keys omitted or null) means every open position -- identical to /orders/exit-all.
    """

    instrument_keys: Optional[list[str]] = None


class AttachGttExitsRequest(BaseModel):
    """Attaches a target and a stoploss to an already-open position with no existing GTT bracket,
    without re-entering. See SmartOrderService.attach_gtt_exits.
    """

    instrument_key: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    product: Literal["I", "D", "MTF"] = "I"
    exit_transaction_type: Literal["BUY", "SELL"]
    target_trigger_price: float = Field(gt=0)
    stoploss_trigger_price: float = Field(gt=0)
    # Overrides the instrument's freeze-quantity-based auto-slicing when set -- same convention
    # as SmartBracketOrderRequest.slice_quantity.
    slice_quantity: Optional[int] = Field(default=None, gt=0)


class ModifyOrderRequest(BaseModel):
    """Fields accepted by the Upstox V3 modify-order endpoint."""

    order_id: str = Field(min_length=1)
    validity: Literal["DAY", "IOC"]
    price: float = Field(ge=0)
    order_type: Literal["MARKET", "LIMIT", "SL", "SL-M"]
    trigger_price: float = Field(ge=0)
    quantity: Optional[int] = Field(default=None, gt=0)
    disclosed_quantity: Optional[int] = Field(default=None, ge=0)
    market_protection: Optional[int] = Field(default=None, ge=-1, le=25)


class ModifyOrdersRequest(BaseModel):
    """A non-empty collection with no application-level order-count cap."""

    orders: list[ModifyOrderRequest] = Field(min_length=1)


@protected_router.get("/status")
def get_status() -> dict[str, str]:
    """Return a basic API status payload for the mobile client."""
    return {"status": "ready"}


@protected_router.get("/auth/login-url")
def get_login_url(
    state: Optional[str] = None,
    service: UpstoxService = Depends(get_upstox_service),
) -> dict[str, str]:
    """Return the Upstox OAuth login URL for the mobile client."""
    try:
        return {"login_url": service.build_login_url(state=state)}
    except AppConfigError as exc:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc


@public_router.get("/auth/callback")
async def auth_callback(
    code: str,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Exchange the Upstox OAuth code, persist the encrypted token, then redirect the in-app
    browser to the mobile app's own custom-scheme URL (settings.mobile_app_redirect_url).

    FIX: this used to return a bare `{"status": "authenticated"}` JSON body, which just sat there
    rendered as raw text in the Chrome Custom Tab the app opened for login -- nothing ever told
    that tab to close, so the user was stuck manually swiping it away and then had to remember to
    tap "check connection" themselves. Redirecting to a URL in the app's own registered scheme
    makes Android hand the tab off to the app directly (closing the tab as part of that handoff,
    same mechanism every other app's in-browser OAuth flow relies on) -- see
    `ConnectViewModel`/`MainActivity`'s matching intent-filter in the Android app repo, which
    reacts to this by re-checking connection status automatically.
    """
    try:
        token_payload = await service.exchange_code_for_token(code)
        token_store.save(token_payload)
    except (AppConfigError, TokenStoreError) as exc:
        return RedirectResponse(f"{settings.mobile_app_redirect_url}?status=error&message={quote(str(exc))}")
    except UpstoxApiError as exc:
        return RedirectResponse(f"{settings.mobile_app_redirect_url}?status=error&message={quote(exc.message)}")
    return RedirectResponse(f"{settings.mobile_app_redirect_url}?status=success")


@protected_router.get("/auth/status")
async def auth_status(
    token_store: EncryptedTokenStore = Depends(get_token_store),
    service: UpstoxService = Depends(get_upstox_service),
) -> dict[str, bool]:
    """Report whether the stored Upstox token is actually still valid.

    FIX: this used to only check `token_store.has_token()` -- whether an encrypted token *file*
    exists -- which stays true even after Upstox's nightly token expiry, since only a fresh login
    overwrites/deletes that file. The Connect screen was using this to show "Connected and
    ready" on a genuinely expired token, with the user only finding out something was wrong when
    an actual trading call failed with UDAPI100050 ("Invalid token"). Now this makes a real,
    cheap authenticated call (get_profile) so an expired token is reported truthfully.
    """
    try:
        if not token_store.has_token():
            return {"authenticated": False}
        access_token = token_store.load_access_token()
    except TokenStoreError as exc:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

    try:
        await service.get_profile(access_token)
    except UpstoxApiError:
        return {"authenticated": False}
    return {"authenticated": True}


@protected_router.post("/auth/logout")
def logout(
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, str]:
    """Clear the encrypted Upstox token."""
    try:
        token_store.clear()
    except TokenStoreError as exc:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    return {"status": "logged_out"}


@protected_router.get("/market/ltp")
async def get_ltp(
    instrument_key: str = Query(min_length=1),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return LTP market data from Upstox."""
    access_token = _load_access_token(token_store)
    try:
        return await service.get_ltp(access_token, instrument_key)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/market/quotes")
async def get_quotes(
    instrument_key: str = Query(min_length=1),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return full market quotes from Upstox."""
    access_token = _load_access_token(token_store)
    try:
        return await service.get_quotes(access_token, instrument_key)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/market/usd-inr")
async def market_usd_inr(service: UsdInrService = Depends(get_usd_inr_service)) -> dict[str, Any]:
    """Best-effort USD/INR quote from a free non-Upstox source (Yahoo Finance's unofficial chart
    endpoint) -- Upstox's own quotes/LTP endpoints reject USD INR outright. Not accurate/official,
    just roughly current; degrades to null fields (never an HTTP error) if Yahoo is unreachable or
    its response shape changes, since this is a "nice to have" ticker entry, not core trading data.
    No Upstox access token needed -- this route doesn't touch the user's Upstox account at all.
    """
    quote = await service.get_quote()
    return {
        "ltp": quote["ltp"] if quote else None,
        "previous_close": quote["previous_close"] if quote else None,
    }


@protected_router.get("/market/oi-analysis")
async def get_oi_analysis(
    expiry: str = Query(min_length=1),
    analysis_date: date = Query(alias="date"),
    instrument_key: str = DEFAULT_UNDERLYING_KEY,
    change_interval: int = Query(default=1, gt=0),
    bucket_interval: int = Query(default=60, gt=0),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return OI, change in OI, max pain, and PCR analysis in one response."""
    access_token = _load_access_token(token_store)
    try:
        return await OIAnalysisService(service).get_analysis(
            access_token,
            instrument_key=instrument_key,
            expiry=expiry,
            date=analysis_date.isoformat(),
            change_interval=change_interval,
            bucket_interval=bucket_interval,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/charges/brokerage")
async def get_brokerage(
    instrument_key: str = Query(min_length=1),
    quantity: int = Query(gt=0),
    product: Literal["I", "D", "MTF"] = Query(),
    transaction_type: Literal["BUY", "SELL"] = Query(),
    price: float = Query(gt=0),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return Upstox's estimated brokerage, taxes, and other charges for one order."""
    access_token = _load_access_token(token_store)
    try:
        return await service.get_brokerage(
            access_token,
            instrument_key=instrument_key,
            quantity=quantity,
            product=product,
            transaction_type=transaction_type,
            price=price,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/portfolio/holdings")
async def get_holdings(
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return long-term holdings from Upstox."""
    access_token = _load_access_token(token_store)
    try:
        return await service.get_holdings(access_token)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/portfolio/positions")
async def get_positions(
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return current positions from Upstox."""
    access_token = _load_access_token(token_store)
    try:
        return await service.get_positions(access_token)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/user/get-funds-and-margin")
async def get_funds_and_margin(
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return the raw Upstox V3 funds-and-margin payload."""
    access_token = _load_access_token(token_store)
    try:
        return await service.get_funds_and_margin(access_token)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/main/bootstrap")
async def main_bootstrap(
    underlying_key: str = DEFAULT_UNDERLYING_KEY,
    expiry_date: Optional[str] = None,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return screen-ready initial data for the option trading main screen."""
    access_token = _load_access_token(token_store)
    try:
        return await MainScreenService(service).bootstrap(
            access_token,
            underlying_key=underlying_key,
            expiry_date=expiry_date,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/main/selected-quote")
async def main_selected_quote(
    expiry_date: str = Query(min_length=1),
    strike_price: float = Query(gt=0),
    option_type: str = Query(pattern="^(CE|PE|ce|pe)$"),
    underlying_key: str = DEFAULT_UNDERLYING_KEY,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return underlying spot plus selected option bid/ask prices."""
    access_token = _load_access_token(token_store)
    try:
        return await MainScreenService(service).selected_quote(
            access_token,
            underlying_key=underlying_key,
            expiry_date=expiry_date,
            strike_price=strike_price,
            option_type=option_type,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/main/option-chain")
async def main_option_chain(
    expiry_date: str = Query(min_length=1),
    underlying_key: str = DEFAULT_UNDERLYING_KEY,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return every strike's live CE/PE market data + option greeks for the underlying + expiry."""
    access_token = _load_access_token(token_store)
    try:
        return await MainScreenService(service).option_chain(
            access_token,
            underlying_key=underlying_key,
            expiry_date=expiry_date,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/main/position-quotes")
async def main_position_quotes(
    instrument_keys: str = Query(default=""),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return LTP snapshots for open positions tracked by the app."""
    access_token = _load_access_token(token_store)
    keys = [key.strip() for key in instrument_keys.split(",") if key.strip()]
    try:
        return await MainScreenService(service).position_quotes(
            access_token,
            instrument_keys=keys,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/main/summary")
async def main_summary(
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return opening balance, current P&L, and closing balance."""
    access_token = _load_access_token(token_store)
    try:
        return await MainScreenService(service).summary(access_token)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/main/underlying-signals")
async def main_underlying_signals(
    underlying_key: str = DEFAULT_UNDERLYING_KEY,
    expiry_date: Optional[str] = None,
    underlying_symbol: Optional[str] = None,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return 9 EMA (5m/15m)/ATR(14)/opening-range/crucial-level/PCR/max-pain/VWAP tags for the
    underlying -- shown to the user just before they place a strike order. See
    UnderlyingSignalsService's doc comment for why this is computed on the underlying itself, not
    the option contract being traded. `expiry_date` is optional -- omitting it just skips the
    PCR/max-pain tags (which need an expiry to ask Upstox's OI endpoints about), everything else
    still works. `underlying_symbol` is likewise optional -- omitting it just skips the VWAP tag
    (computed from the underlying's own futures contract, resolved by a symbol-text search since
    Upstox has no search-by-instrument_key mode), everything else still works.
    """
    access_token = _load_access_token(token_store)
    try:
        return await UnderlyingSignalsService(service).get_signals(
            access_token,
            underlying_key=underlying_key,
            expiry_date=expiry_date,
            underlying_symbol=underlying_symbol,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/user/tracked-instruments")
async def get_tracked_instruments(
    store: TrackedInstrumentsStore = Depends(get_tracked_instruments_store),
) -> dict[str, Any]:
    """Return the persisted list of underlying_keys the background poller keeps 5-minute-change
    history warm for -- lets the Settings screen show the current selection on load."""
    return {"underlying_keys": store.load()}


@protected_router.put("/user/tracked-instruments")
async def set_tracked_instruments(
    body: TrackedInstrumentsRequest,
    store: TrackedInstrumentsStore = Depends(get_tracked_instruments_store),
) -> dict[str, Any]:
    """Replace the whole persisted set -- see TrackedInstrumentsRequest. Picking instruments here
    (in the app's Settings screen) means the background poller (see app.main's lifespan) keeps
    that underlying's PCR/OI/ATM-straddle/VWAP/ATR 5-minute history warm even while the app is
    closed, so opening the app later shows a delta on the very first poll instead of needing 5
    live minutes first -- see UnderlyingSignalsService._record_and_diff.
    """
    try:
        store.save(body.underlying_keys)
    except TrackedInstrumentsStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": "error", "message": str(exc)},
        ) from exc
    return {"underlying_keys": store.load()}


@protected_router.get("/market/feed/authorize")
async def authorize_market_feed(
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return a one-time Upstox V3 market feed WebSocket URL."""
    access_token = _load_access_token(token_store)
    try:
        return await service.get_market_feed_authorize(access_token)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/search/underlyings")
async def search_underlyings(
    query: str = Query(default="", max_length=50),
    limit: int = Query(default=20, ge=1, le=30),
    page_number: int = Query(default=1, ge=1),
    include_futures: bool = Query(default=False),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Search option-capable index/equity underlyings, optionally also matching futures contracts
    (see SearchScreenService.search_underlyings' doc comment for why include_futures is opt-in).
    """
    access_token = _load_access_token(token_store)
    try:
        return await SearchScreenService(service).search_underlyings(
            access_token,
            query=query,
            limit=limit,
            page_number=page_number,
            include_futures=include_futures,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/orders/history")
async def order_history(
    scope: str = Query(default="today", pattern="^(today|all)$"),
    page_number: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=500),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    segment: str = Query(default="FO", pattern="^(EQ|FO|CD|COM|MF)$"),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return paginated order-history screen data."""
    access_token = _load_access_token(token_store)
    order_service = OrderHistoryService(service)
    try:
        if scope == "today":
            return await order_service.today_orders(
                access_token,
                page_number=page_number,
                page_size=page_size,
            )
        return await order_service.historical_orders(
            access_token,
            page_number=page_number,
            page_size=page_size,
            start_date=start_date,
            end_date=end_date,
            segment=segment,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.post("/orders/smart-bracket")
async def place_smart_bracket_order(
    order: SmartBracketOrderRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Place a bracket-like order using Upstox multi-leg GTT."""
    access_token = _load_access_token(token_store)
    try:
        rules = await InstrumentRulesService(settings).get_rules(order.instrument_key)
        validate_quantity(order.quantity, rules)
        validate_price(order.entry_trigger_price, rules, field_name="entry_trigger_price")
        validate_price(order.target_trigger_price, rules, field_name="target_trigger_price")
        validate_price(order.stoploss_trigger_price, rules, field_name="stoploss_trigger_price")
        slice_quantity = order.slice_quantity or slice_quantity_for_freeze(order.quantity, rules)
        return await SmartOrderService(service).place_bracket_order(
            access_token,
            instrument_key=order.instrument_key,
            transaction_type=order.transaction_type,
            quantity=order.quantity,
            product=order.product,
            entry_trigger_type=order.entry_trigger_type,
            entry_trigger_price=order.entry_trigger_price,
            target_trigger_price=order.target_trigger_price,
            stoploss_trigger_price=order.stoploss_trigger_price,
            trailing_gap=order.trailing_gap,
            market_protection=order.market_protection,
            slice_quantity=slice_quantity,
        )
    except AppConfigError as exc:
        raise _http_error(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/orders/gtt")
async def get_gtt_orders(
    instrument_key: str = Query(..., min_length=1),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> list[dict[str, Any]]:
    """Active GTT orders for one instrument -- lets the app find the bracket order behind an open
    position so its target/stoploss can be shown and edited. See
    SmartOrderService.get_gtt_orders_for_instrument.
    """
    access_token = _load_access_token(token_store)
    try:
        return await SmartOrderService(service).get_gtt_orders_for_instrument(
            access_token, instrument_key=instrument_key
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.put("/orders/gtt/modify")
async def modify_gtt_order(
    order: ModifyGttOrderRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Re-points an existing GTT bracket's target/stoploss. See SmartOrderService.modify_gtt_bracket."""
    access_token = _load_access_token(token_store)
    try:
        rules = await InstrumentRulesService(settings).get_rules(order.instrument_key)
        validate_price(order.target_trigger_price, rules, field_name="target_trigger_price")
        validate_price(order.stoploss_trigger_price, rules, field_name="stoploss_trigger_price")
        return await SmartOrderService(service).modify_gtt_bracket(
            access_token,
            gtt_order_id=order.gtt_order_id,
            quantity=order.quantity,
            product=order.product,
            entry_trigger_type=order.entry_trigger_type,
            entry_trigger_price=order.entry_trigger_price,
            target_trigger_price=order.target_trigger_price,
            stoploss_trigger_price=order.stoploss_trigger_price,
            trailing_gap=order.trailing_gap,
        )
    except AppConfigError as exc:
        raise _http_error(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.post("/orders/gtt/attach-exits")
async def attach_gtt_exits(
    order: AttachGttExitsRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Attaches a target/stoploss to a position with no existing GTT bracket. See
    SmartOrderService.attach_gtt_exits.
    """
    access_token = _load_access_token(token_store)
    try:
        rules = await InstrumentRulesService(settings).get_rules(order.instrument_key)
        validate_quantity(order.quantity, rules)
        validate_price(order.target_trigger_price, rules, field_name="target_trigger_price")
        validate_price(order.stoploss_trigger_price, rules, field_name="stoploss_trigger_price")
        slice_quantity = order.slice_quantity or slice_quantity_for_freeze(order.quantity, rules)
        return await SmartOrderService(service).attach_gtt_exits(
            access_token,
            instrument_key=order.instrument_key,
            quantity=order.quantity,
            product=order.product,
            exit_transaction_type=order.exit_transaction_type,
            target_trigger_price=order.target_trigger_price,
            stoploss_trigger_price=order.stoploss_trigger_price,
            slice_quantity=slice_quantity,
        )
    except AppConfigError as exc:
        raise _http_error(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.post("/orders/exit-all")
async def exit_all_positions(
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Flattens every currently open position with an immediate market order -- backs the app's
    max-loss auto square-off. See SmartOrderService.exit_all_positions.
    """
    access_token = _load_access_token(token_store)
    try:
        return await SmartOrderService(service).exit_all_positions(
            access_token, instrument_rules_service=InstrumentRulesService(settings)
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.post("/orders/exit-positions")
async def exit_positions(
    request: ExitPositionsRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Flattens open positions with an immediate market order, optionally scoped to
    [ExitPositionsRequest.instrument_keys] (e.g. "close only profitable positions", computed
    client-side). See SmartOrderService.exit_positions.
    """
    access_token = _load_access_token(token_store)
    try:
        return await SmartOrderService(service).exit_positions(
            access_token,
            instrument_keys=request.instrument_keys,
            instrument_rules_service=InstrumentRulesService(settings),
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.put("/orders/modify")
async def modify_orders(
    request: ModifyOrdersRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Modify any number of open or pending orders."""
    access_token = _load_access_token(token_store)
    orders = [order.model_dump(exclude_none=True) for order in request.orders]
    return await OrderModificationService(service).modify_orders(access_token, orders)


def _load_access_token(token_store: EncryptedTokenStore) -> str:
    """Load the stored token or convert storage failures into API errors."""
    try:
        return token_store.load_access_token()
    except UpstoxAuthRequiredError as exc:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except TokenStoreError as exc:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc


def _http_error(status_code: int, message: str) -> HTTPException:
    """Build a normalized HTTP error response."""
    return HTTPException(
        status_code=status_code,
        detail={"status": "error", "message": message},
    )


def _upstox_http_error(exc: UpstoxApiError) -> HTTPException:
    """Build a normalized HTTP response for an Upstox API failure.

    Logged here (not just returned to the client) because Upstox's raw response body was
    previously undiagnosable from `docker compose logs` -- uvicorn's access log only records the
    resulting status code (e.g. "GET /api/main/bootstrap ... 423 Locked"), never the body Upstox
    actually sent back explaining *why*. The Android app now also surfaces `exc.details` in its
    own error message (see the app repo's `ApiResult.parseErrorBody`), but logging it here too
    means it's visible without needing a client rebuild to see it.
    """
    logger.error(
        "Upstox API failure: status_code=%s upstox_code=%s message=%s details=%s",
        exc.status_code,
        exc.upstox_code,
        exc.message,
        exc.details,
    )
    detail: dict[str, Any] = {
        "status": "error",
        "message": exc.message,
        "upstox_code": exc.upstox_code,
    }
    if exc.details is not None:
        detail["details"] = exc.details
    return HTTPException(status_code=exc.status_code, detail=detail)


router = APIRouter()
router.include_router(public_router)
router.include_router(protected_router)
