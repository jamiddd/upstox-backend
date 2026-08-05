from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import date, datetime, timezone
import logging
from typing import Any, Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.api.dependencies import (
    get_candle_cache_store,
    get_device_token_store,
    get_exit_all_lock,
    get_journal_store,
    get_max_loss_settings_store,
    get_notification_service,
    get_notification_store,
    get_oi_snapshot_store,
    get_signal_snapshot_store,
    get_token_store,
    get_tracked_instruments_store,
    get_upstox_service,
    get_usd_inr_service,
    get_watchlist_store,
)
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AppConfigError,
    TokenStoreError,
    TrackedInstrumentsStoreError,
    UpstoxApiError,
    UpstoxAuthRequiredError,
    UpstoxAutoLoginError,
    WatchlistStoreError,
)
from app.services.token_store import EncryptedTokenStore
from app.services.candle_service import CandleService
from app.services.candle_cache_store import CandleCacheStore
from app.core.web_session import (
    WEB_SESSION_COOKIE_NAME,
    DEFAULT_SESSION_TTL_SECONDS,
    create_session_token,
)
from app.services.tracked_instruments_store import TrackedInstrumentsStore
from app.services.upstox_service import UpstoxService
from app.services.watchlist_store import WatchlistStore
from app.core.security import require_mobile_api_key, require_mobile_or_web, require_web_session
from app.services.instrument_rules_service import (
    InstrumentRulesService,
    slice_quantity_for_freeze,
    validate_price,
    validate_quantity,
)
from app.services.device_token_store import DeviceTokenStore
from app.services.max_loss_settings_store import MaxLossSettingsStore
from app.services.main_screen_service import DEFAULT_UNDERLYING_KEY, MainScreenService
from app.services.notification_service import NotificationService
from app.services.notification_store import NotificationStore
from app.services.journal_store import DuplicateJournalTradeError, JournalStore
from app.services.gtt_history_store import GttHistoryStore
from app.services.order_history_service import OrderHistoryService
from app.services.order_cancellation_service import OrderCancellationService
from app.services.order_modification_service import OrderModificationService
from app.services.account_snapshot_store import AccountSnapshotStore
from app.services.oi_analysis_service import OIAnalysisService
from app.services.oi_snapshot_store import OISnapshotStore, SnapshotNotFoundError
from app.services.search_screen_service import SearchScreenService
from app.services.signal_snapshot_store import SignalSnapshotStore
from app.services.smart_order_service import SmartOrderService
from app.services import quantity_sizing
from app.services.underlying_signals_service import UnderlyingSignalsService
from app.services.trade_context_service import TradeContextService, extract_order_ids
from app.services.usd_inr_service import UsdInrService
from app.services.upstox_totp_login import UpstoxTotpLoginService

public_router = APIRouter()
protected_router = APIRouter(dependencies=[Depends(require_mobile_api_key)])
# Routes only the web client calls with its session cookie -- kept on a separate router (rather
# than adding require_web_session route-by-route to protected_router) since protected_router's
# router-level dependency is require_mobile_api_key for every route included in it; Android never
# touches this router at all.
web_router = APIRouter(dependencies=[Depends(require_web_session)])
# Routes both Android (X-API-Key) and the web client (session cookie) call -- a route needing
# either auth can't stay on protected_router (its router-level dependency is require_mobile_api_key
# only), so it moves here instead. Only routes that actually need to be reachable from the browser
# move onto this router; everything else stays on protected_router until it does.
dual_router = APIRouter(dependencies=[Depends(require_mobile_or_web)])

logger = logging.getLogger(__name__)


class SmartBracketOrderRequest(BaseModel):
    """Client-provided bracket-like GTT order parameters."""

    instrument_key: str = Field(min_length=1)
    underlying_key: Optional[str] = Field(default=None, min_length=1)
    signal_expiry_date: Optional[str] = None
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


class SuggestedQuantityRequest(BaseModel):
    """Request body for QuantitySizing.kt's server-side port -- see
    app/services/quantity_sizing.py. Mirrors default_quantity's parameters; all optional except
    mode/lot_size, matching the Kotlin function's own nullable-inputs-mean-fallback behavior."""

    held_quantity: int = 0
    mode: Literal["FIXED", "CAPITAL_BASED", "RISK_BASED", "ATR_BASED", "IV_BASED", "KELLY"] = "FIXED"
    available_capital: Optional[float] = None
    capital_allocation_percent: float = 0
    buffer_amount: float = 0
    estimated_charges: Optional[float] = None
    entry_price: Optional[float] = Field(default=None, gt=0)
    lot_size: int = Field(gt=0)
    default_lot_count: int = 1
    risk_per_trade_amount: float = 0
    risk_management_is_percent: bool = True
    stop_loss_value: float = 0
    atr_14_5m: Optional[float] = None
    contract_delta: Optional[float] = None
    contract_iv: Optional[float] = None
    atr_stop_multiplier: float = 1.5
    iv_stop_multiplier: float = 1.0
    kelly_trade_count: int = 0
    kelly_win_rate: Optional[float] = None
    kelly_average_win: Optional[float] = None
    kelly_average_loss: Optional[float] = None
    kelly_capital: Optional[float] = None


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


class CancelGttOrderRequest(BaseModel):
    """Identifies the complete GTT order whose remaining rules should be cancelled."""

    gtt_order_id: str = Field(min_length=1)


class TrackedInstrumentsRequest(BaseModel):
    """Replaces the whole persisted set of underlying_keys the background poller keeps
    5-minute-change history warm for -- see TrackedInstrumentsStore. Always the client's full
    current Settings selection, not an incremental add/remove.
    """

    underlying_keys: list[str] = Field(default_factory=list)


class WatchlistInstrumentModel(BaseModel):
    """One entry in a watchlist -- see WatchlistRequest/WatchlistStore."""

    instrument_key: str
    symbol: str
    lot_size: Optional[float] = None
    is_underlying: bool = False


class WatchlistRequest(BaseModel):
    """Replaces the whole persisted watchlist for one list_id ("india" or "global") -- see
    WatchlistStore. Always the client's full current list, not an incremental add/remove, same
    contract as TrackedInstrumentsRequest.
    """

    items: list[WatchlistInstrumentModel] = Field(default_factory=list)


class ExitPositionsRequest(BaseModel):
    """Optionally scopes /orders/exit-positions to a subset of open positions. None
    (instrument_keys omitted or null) means every open position -- identical to /orders/exit-all.
    """

    instrument_keys: Optional[list[str]] = None


class MaxLossSettingsRequest(BaseModel):
    """Body for `PUT /settings/max-loss`. `amount <= 0` disables the backend's own watcher, same
    convention as AppSettingsRepository.maxLossAmount on the client."""

    amount: float = Field(ge=0)


class MarginRequest(BaseModel):
    """Body for `POST /charges/margin` -- unlike /charges/brokerage (plain query params, since
    it's a GET), the upstream Margin Calculator API takes a JSON body natively (a batch of up to
    20 instruments), so this mirrors that shape for the single instrument this app ever prices
    at once instead of forcing it through query params.
    """

    instrument_key: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    product: Literal["I", "D", "MTF"] = "I"
    transaction_type: Literal["BUY", "SELL"]
    price: float = Field(default=0, ge=0)


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


class CancelOrdersRequest(BaseModel):
    """A non-empty collection of still-open order ids to cancel, same best-effort shape as
    ModifyOrdersRequest -- one order failing to cancel doesn't stop the rest.
    """

    order_ids: list[str] = Field(min_length=1)


@protected_router.get("/status")
def get_status() -> dict[str, str]:
    """Return a basic API status payload for the mobile client."""
    return {"status": "ready"}


@protected_router.get("/debug/feed-status")
def get_feed_status(request: Request) -> dict[str, Any]:
    """Read-only snapshot of the market-feed connection/subscription state -- lets a reported
    "this contract's price is frozen" incident be cross-checked directly against the backend's
    own view (is it still in the desired set, how long since its own last tick, how close the
    full-mode union is to Upstox's 50-key cap) without needing server/log file access."""
    market_feed_client = getattr(request.app.state, "market_feed_client", None)
    subscription_manager = getattr(request.app.state, "feed_subscription_manager", None)
    stream_manager = getattr(request.app.state, "stream_manager", None)
    if market_feed_client is None or subscription_manager is None or stream_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "error", "message": "Market feed not initialized yet"},
        )
    return {
        "market_feed": market_feed_client.debug_snapshot(),
        "subscription_manager": subscription_manager.debug_snapshot(),
        "stream_manager": stream_manager.debug_snapshot(),
    }


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


@protected_router.post("/auth/web-login")
def web_login(
    response: Response,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Issue the web client's signed session cookie.

    Protected by the same MOBILE_API_KEY every other protected_router route already requires --
    the browser must have that key entered once (mirroring Android's own "Connect" pairing) before
    it can mint a session. The cookie itself is what subsequent requests/`/stream` use afterward,
    so the browser never needs to hold the raw API key beyond this one call.
    """
    try:
        settings.require_web_session_secret()
    except AppConfigError as exc:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

    token = create_session_token(settings)
    response.set_cookie(
        key=WEB_SESSION_COOKIE_NAME,
        value=token,
        max_age=DEFAULT_SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return {"status": "ok"}


@web_router.post("/auth/web-logout")
def web_logout(response: Response) -> dict[str, str]:
    """Clear the web client's session cookie.

    On web_router (require_web_session), not protected_router (require_mobile_api_key) -- by the
    time a browser wants to log out, it only ever holds the session cookie from web-login, never
    the raw MOBILE_API_KEY (see web_login's own doc comment for why). Gating logout behind the API
    key would make it permanently unreachable from the web client.
    """
    response.delete_cookie(key=WEB_SESSION_COOKIE_NAME)
    return {"status": "logged_out"}


@web_router.get("/auth/web-session-status")
def web_session_status() -> dict[str, bool]:
    """Report whether the calling browser's session cookie is currently valid.

    Reaching this handler at all already proves the cookie is valid -- web_router's
    require_web_session dependency 401s before this body ever runs otherwise -- so the response is
    always `{"authenticated": true}`. Distinct from `/auth/status` (which reports whether the
    *Upstox* OAuth token is still valid) -- this is purely "is this browser's own session with our
    backend still good," the thing the protected shell needs to decide whether to redirect to
    `/login`.
    """
    return {"authenticated": True}


@public_router.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: str,
    state: Optional[str] = None,
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

    `state == "web"` (threaded through from `GET /auth/login-url`'s own optional `state` param)
    redirects to `settings.web_client_auth_redirect_url` instead -- lets the web client reuse this
    same Upstox-registered redirect URL rather than needing a second one registered with Upstox.
    Any other/missing state value keeps today's Android behavior as the default.
    """
    redirect_base = (
        settings.web_client_auth_redirect_url if state == "web" else settings.mobile_app_redirect_url
    )
    try:
        token_payload = await service.exchange_code_for_token(code)
        token_store.save(token_payload)
        reconciler = getattr(request.app.state, "journal_reconciler", None)
        if reconciler is not None:
            asyncio.create_task(reconciler.reconcile())
    except (AppConfigError, TokenStoreError) as exc:
        return RedirectResponse(f"{redirect_base}?status=error&message={quote(str(exc))}")
    except UpstoxApiError as exc:
        return RedirectResponse(f"{redirect_base}?status=error&message={quote(exc.message)}")
    return RedirectResponse(f"{redirect_base}?status=success")


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

@protected_router.post("/auth/auto-login")                                                                    
async def auto_login(                                                                                         
      request: Request,                                                                                         
      service: UpstoxService = Depends(get_upstox_service),                                                     
      token_store: EncryptedTokenStore = Depends(get_token_store),                                              
      settings: Settings = Depends(get_settings),                                                               
) -> dict[str, str]:                                                                                          
      """Manually trigger the same automated TOTP login `auto_login_scheduler` runs every morning               
      (see `UpstoxTotpLoginService`) -- for testing, and as an on-demand "retry now" affordance                 
      without waiting for the next scheduled attempt. Does not count against                                    
      `auto_login_scheduler`'s own daily attempt cap, since a human explicitly asking for this                  
      isn't the "something is silently broken and retrying" case that cap guards against.                       
      """                                                                                                       
      login_service = UpstoxTotpLoginService(settings, service)                                                 
      try:                                                                                                      
          token_payload = await login_service.login()                                                           
      except (AppConfigError, UpstoxAutoLoginError) as exc:                                                     
          raise _http_error(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc                                     
      except UpstoxApiError as exc:                                                                             
          raise _upstox_http_error(exc) from exc                                                                
      try:                                                                                                      
          token_store.save(token_payload)                                                                       
      except TokenStoreError as exc:                                                                            
          raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc                           
      reconciler = getattr(request.app.state, "journal_reconciler", None)                                       
      if reconciler is not None:                                                                                
          asyncio.create_task(reconciler.reconcile())                                                           
      return {"status": "authenticated"} 


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


@dual_router.get("/market/oi-analysis")
async def get_oi_analysis(
    expiry: str = Query(min_length=1),
    analysis_date: date = Query(alias="date"),
    instrument_key: str = DEFAULT_UNDERLYING_KEY,
    change_interval: int = Query(default=1, gt=0),
    bucket_interval: int = Query(default=60, gt=0),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return OI, change in OI, max pain, and PCR analysis in one response.

    On dual_router (require_mobile_or_web) -- backs the web client's Option Chain screen summary
    row (total Call/Put OI, PCR); was Android-only (protected_router) which left that row on the
    web client permanently unpopulated since the browser can't set the X-API-Key header.
    """
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


@protected_router.post("/charges/margin")
async def get_margin(
    request: MarginRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return Upstox's actual required margin for one proposed order -- the real fund block for
    a SELL (short options), unlike /charges/brokerage's statutory-charges-only figure.
    """
    access_token = _load_access_token(token_store)
    try:
        return await service.get_margin(
            access_token,
            instrument_key=request.instrument_key,
            quantity=request.quantity,
            product=request.product,
            transaction_type=request.transaction_type,
            price=request.price,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@dual_router.get("/portfolio/holdings")
async def get_holdings(
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return long-term holdings from Upstox.

    On dual_router (require_mobile_or_web) -- the web client's Portfolio screen (M1) needs this.
    """
    access_token = _load_access_token(token_store)
    try:
        return await service.get_holdings(access_token)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@dual_router.get("/portfolio/positions")
async def get_positions(
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return current positions from Upstox.

    On dual_router (require_mobile_or_web) -- the web client's Portfolio screen (M1) needs this.
    """
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


@dual_router.get("/main/bootstrap")
async def main_bootstrap(
    underlying_key: str = DEFAULT_UNDERLYING_KEY,
    expiry_date: Optional[str] = None,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
    journal_store: JournalStore = Depends(get_journal_store),
) -> dict[str, Any]:
    """Return screen-ready initial data for the option trading main screen.

    On dual_router (require_mobile_or_web), not protected_router -- this is the web client's Main
    dashboard screen (M1), so it must accept the browser's session cookie as well as Android's
    X-API-Key header.
    """
    access_token = _load_access_token(token_store)
    try:
        return await MainScreenService(service, AccountSnapshotStore(settings), journal_store).bootstrap(
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


@dual_router.get("/main/option-chain")
async def main_option_chain(
    expiry_date: str = Query(min_length=1),
    underlying_key: str = DEFAULT_UNDERLYING_KEY,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return every strike's live CE/PE market data + option greeks (+ lot_size) for the
    underlying + expiry.

    On dual_router (require_mobile_or_web) -- backs the web client's Option Chain, GEX, and OI
    screens (M1), all three of which poll this same endpoint and compute their own thing
    client-side, mirroring Android's OptionChainViewModel/GexViewModel/OiViewModel.
    """
    access_token = _load_access_token(token_store)
    try:
        return await MainScreenService(service).option_chain(
            access_token,
            underlying_key=underlying_key,
            expiry_date=expiry_date,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@dual_router.get("/main/position-quotes")
async def main_position_quotes(
    instrument_keys: str = Query(default=""),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return LTP snapshots for open positions tracked by the app. On dual_router
    (require_mobile_or_web) -- the web client's TickerBar (M5c-equivalent) needs this to hydrate
    an instant snapshot on mount instead of waiting for each instrument's next live WS tick,
    same reasoning Android's own toolbar ticker poll already relies on this endpoint for."""
    access_token = _load_access_token(token_store)
    keys = [key.strip() for key in instrument_keys.split(",") if key.strip()]
    try:
        return await MainScreenService(service).position_quotes(
            access_token,
            instrument_keys=keys,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@dual_router.get("/main/summary")
async def main_summary(
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
    journal_store: JournalStore = Depends(get_journal_store),
) -> dict[str, Any]:
    """Return opening balance, current P&L, and closing balance.

    On dual_router (require_mobile_or_web) -- the web client's Analytics screen (M1) needs this
    for its capital_base denominator.
    """
    access_token = _load_access_token(token_store)
    try:
        return await MainScreenService(service, AccountSnapshotStore(settings), journal_store).summary(access_token)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/main/underlying-signals")
async def main_underlying_signals(
    underlying_key: str = DEFAULT_UNDERLYING_KEY,
    expiry_date: Optional[str] = None,
    underlying_symbol: Optional[str] = None,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    snapshot_store: SignalSnapshotStore = Depends(get_signal_snapshot_store),
    oi_snapshot_store: OISnapshotStore = Depends(get_oi_snapshot_store),
) -> dict[str, Any]:
    """Return 9 EMA (5m/15m)/ATR(14)/opening-range/crucial-level/PCR/max-pain/VWAP tags for the
    underlying -- shown to the user just before they place a strike order. See
    UnderlyingSignalsService's doc comment for why this is computed on the underlying itself, not
    the option contract being traded. `expiry_date` is optional -- omitting it just skips the
    PCR/max-pain tags (which need an expiry to ask Upstox's OI endpoints about), everything else
    still works. `underlying_symbol` is likewise optional -- omitting it just skips the VWAP tag
    (computed from the underlying's own futures contract, resolved by a symbol-text search since
    Upstox has no search-by-instrument_key mode), everything else still works.

    `oi_snapshot_store` lets OI(S)/OI(R)'s 5-minute-change figures use the same per-strike history
    the Open Interest chart and `oi_snapshot_collector`'s background poller already read/write --
    see `UnderlyingSignalsService._oi_analysis`'s doc comment. This means every live call here
    (for *any* underlying being viewed, tracked or not) also opportunistically contributes to that
    shared history, not just the background poller's tracked instruments.
    """
    access_token = _load_access_token(token_store)
    try:
        return await UnderlyingSignalsService(
            service, snapshot_store=snapshot_store, oi_snapshot_store=oi_snapshot_store,
        ).get_signals(
            access_token,
            underlying_key=underlying_key,
            expiry_date=expiry_date,
            underlying_symbol=underlying_symbol,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@dual_router.get("/main/underlying-signals/history")
async def main_underlying_signals_history(
    underlying_key: str = Query(min_length=1),
    expiry_date: Optional[str] = None,
    limit: int = Query(default=200, ge=1, le=1000),
    snapshot_store: SignalSnapshotStore = Depends(get_signal_snapshot_store),
) -> dict[str, Any]:
    """Return durable five-minute signal metrics without requiring a live Upstox token.

    On dual_router (require_mobile_or_web), not protected_router -- the web client's Straddle
    screen needs this (it's the only data source for the ATM-straddle history line chart; unlike
    most other web-client routes this needs no live Upstox token at all, just the stored
    snapshots, so there's no server-side reason to keep it mobile-only).
    """
    return {
        "underlying_key": underlying_key,
        "expiry_date": expiry_date,
        "snapshots": snapshot_store.list_snapshots(
            underlying_key=underlying_key,
            expiry_date=expiry_date,
            limit=limit,
        ),
    }


@protected_router.get("/main/oi-snapshots/history")
async def main_oi_snapshots_history(
    underlying_key: str = Query(min_length=1),
    expiry_date: Optional[str] = None,
    limit: int = Query(default=200, ge=1, le=1000),
    snapshot_store: OISnapshotStore = Depends(get_oi_snapshot_store),
) -> dict[str, Any]:
    """Return five-minute OI slot metadata for a picker before requesting a diff.

    Requires no live Upstox token, like ``/main/underlying-signals/history``.
    """
    snapshots = snapshot_store.list_snapshots(
        underlying_key=underlying_key,
        expiry_date=expiry_date,
        limit=limit,
    )
    if expiry_date is not None:
        # The requested expiry is already present at the response root. Keep filtered rows as
        # lightweight as the client contract; cross-expiry rows retain this field for identity.
        snapshots = [
            {key: value for key, value in snapshot.items() if key != "expiry_date"}
            for snapshot in snapshots
        ]
    return {
        "underlying_key": underlying_key,
        "expiry_date": expiry_date,
        "snapshots": snapshots,
    }


@protected_router.get("/main/oi-snapshots/diff")
async def main_oi_snapshots_diff(
    underlying_key: str = Query(min_length=1),
    expiry_date: str = Query(min_length=1),
    from_slot: datetime = Query(...),
    to_slot: datetime = Query(...),
    snapshot_store: OISnapshotStore = Depends(get_oi_snapshot_store),
) -> dict[str, Any]:
    """Return per-strike call/put OI changes between two previously stored slots, plus each
    strike's absolute call/put OI as of `to_slot` (see `OiStrikeDiff.call_oi`/`put_oi`) -- lets a
    caller render this the same way as a plain snapshot (bar height = current level, capped change
    on top of it), not just as a delta-only view."""
    if from_slot.tzinfo is None or from_slot.utcoffset() is None:
        raise _snapshot_diff_validation_error("from_slot must include a timezone offset")
    if to_slot.tzinfo is None or to_slot.utcoffset() is None:
        raise _snapshot_diff_validation_error("to_slot must include a timezone offset")
    if from_slot.microsecond or to_slot.microsecond:
        raise _snapshot_diff_validation_error("Snapshot slots cannot include fractional seconds")
    if to_slot <= from_slot:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"status": "error", "message": "to_slot must be strictly after from_slot"},
        )

    try:
        diff = snapshot_store.diff_strikes(
            underlying_key=underlying_key,
            expiry_date=expiry_date,
            from_slot=from_slot,
            to_slot=to_slot,
        )
    except SnapshotNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"status": "error", "message": str(exc)},
        ) from exc
    return {
        "underlying_key": underlying_key,
        "expiry_date": expiry_date,
        "from_slot": from_slot.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "to_slot": to_slot.astimezone(timezone.utc).isoformat(timespec="seconds"),
        **asdict(diff),
    }


def _snapshot_diff_validation_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"status": "error", "message": message},
    )


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


@dual_router.get("/user/watchlist/{list_id}")
async def get_watchlist(
    list_id: Literal["india", "global"],
    store: WatchlistStore = Depends(get_watchlist_store),
) -> dict[str, Any]:
    """Return the persisted watchlist ("india" or "global") -- shared by both Android's Main
    screen ticker and the web client's TickerBar/WatchlistScreen. On dual_router
    (require_mobile_or_web) since both clients need this."""
    return {"items": store.load(list_id)}


@dual_router.put("/user/watchlist/{list_id}")
async def set_watchlist(
    list_id: Literal["india", "global"],
    body: WatchlistRequest,
    store: WatchlistStore = Depends(get_watchlist_store),
) -> dict[str, Any]:
    """Replace the whole persisted watchlist for list_id -- see WatchlistRequest. Both Android
    and the web client push their full current list here right after every local add/remove/
    reorder; last write wins, same posture as /user/tracked-instruments."""
    try:
        store.save(list_id, [item.model_dump() for item in body.items])
    except WatchlistStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": "error", "message": str(exc)},
        ) from exc
    return {"items": store.load(list_id)}


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


@dual_router.get("/market/candles")
async def market_candles(
    instrument_key: str = Query(min_length=1),
    unit: Literal["minutes", "hours", "days"] = "minutes",
    interval: int = Query(default=5, ge=1, le=300),
    from_date: date = Query(),
    to_date: date = Query(),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    candle_cache_store: Optional[CandleCacheStore] = Depends(get_candle_cache_store),
) -> dict[str, Any]:
    """Return a normalized historical-plus-intraday candle series for the mobile chart.

    On dual_router (require_mobile_or_web) -- the web client's Chart screen (M4a) needs this.
    """
    if from_date > to_date:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "from_date must not be after to_date")
    if unit == "hours" and interval > 5:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Hour intervals must be between 1 and 5")
    if unit == "days" and interval != 1:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Day interval must be 1")
    max_range_days = 730 if unit == "days" else 90 if unit == "hours" or interval > 15 else 31
    if (to_date - from_date).days > max_range_days:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{unit.capitalize()} candle ranges are limited to {max_range_days} days",
        )

    access_token = _load_access_token(token_store)
    try:
        return await CandleService(service, candle_cache_store).get_candles(
            access_token,
            instrument_key=instrument_key,
            unit=unit,
            interval=interval,
            from_date=from_date,
            to_date=to_date,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@dual_router.get("/search/underlyings")
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

    On dual_router (require_mobile_or_web), not protected_router -- this is the web client's
    Search screen (M1), so it must accept the browser's session cookie as well as Android's
    X-API-Key header.
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


@protected_router.get("/search/contracts")
async def search_contracts(
    query: str = Query(min_length=2, max_length=50),
    underlying_key: Optional[str] = Query(default=None, min_length=3, max_length=200),
    limit: int = Query(default=30, ge=1, le=30),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Search actual derivative instruments for canonical manual-journal entry."""
    access_token = _load_access_token(token_store)
    try:
        return await SearchScreenService(service).search_contracts(
            access_token,
            query=query,
            underlying_key=underlying_key,
            limit=limit,
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@dual_router.get("/orders/history")
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
    """Return paginated order-history screen data.

    On dual_router (require_mobile_or_web) -- the web client's read-only Order History screen (M1)
    needs this. Write actions (cancel/modify) stay on protected_router, Android-only.
    """
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


class RegisterDeviceRequest(BaseModel):
    fcm_token: Optional[str] = None
    push_preference: str = Field(pattern="^(off|critical|everything)$")


class JournalNotesRequest(BaseModel):
    setup: Optional[str] = None
    entry_reason: Optional[str] = None
    exit_reason: Optional[str] = None
    plan: Optional[str] = None
    mistakes: Optional[str] = None
    lessons: Optional[str] = None
    notes: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    confidence_rating: Optional[int] = Field(default=None, ge=1, le=5)
    execution_rating: Optional[int] = Field(default=None, ge=1, le=5)
    reviewed: bool = False


class ManualJournalTradeRequest(BaseModel):
    instrument_key: str
    trading_symbol: str
    trade_date: str
    direction: Literal["long", "short"]
    quantity: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    opened_at: str
    closed_at: str
    gross_pnl: float
    charges: float = Field(default=0, ge=0)
    journal: Optional[JournalNotesRequest] = None


@protected_router.get("/notifications")
async def list_notifications(
    category: Optional[str] = None,
    severity: Optional[str] = Query(default=None, pattern="^(info|warning|critical)$"),
    unread_only: bool = False,
    page_number: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    store: NotificationStore = Depends(get_notification_store),
) -> dict[str, Any]:
    """Return the paginated, filterable notification log (see `docs/MAIN_SCREEN_API.md`'s
    Notifications section) -- every backend-generated alert/message the app's Notifications
    screen shows."""
    items, page = store.list_notifications(
        category=category,
        severity=severity,
        unread_only=unread_only,
        page_number=page_number,
        page_size=page_size,
    )
    return {
        "notifications": items,
        "page": page,
        "unread_count": store.unread_count(),
    }


@protected_router.post("/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: int,
    store: NotificationStore = Depends(get_notification_store),
) -> dict[str, Any]:
    """Marks one notification read. Idempotent -- marking an already-read (or nonexistent)
    notification read again just reports `updated: false`, not an error."""
    updated = store.mark_read(notification_id)
    return {"updated": updated, "unread_count": store.unread_count()}


@dual_router.get("/journal/trades")
def list_journal_trades(
    page_number: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    store: JournalStore = Depends(get_journal_store),
) -> dict[str, Any]:
    """On dual_router (require_mobile_or_web) -- the web client's read-only Journal screen (M1)
    needs this. The PATCH/POST journal write routes stay on protected_router, Android-only."""
    trades, page = store.list_trades(
        page_number=page_number, page_size=page_size,
        start_date=start_date, end_date=end_date,
    )
    return {"trades": trades, "page": page}


@protected_router.get("/journal/filter-options")
def journal_filter_options(
    store: JournalStore = Depends(get_journal_store),
) -> dict[str, list[str]]:
    return store.filter_options()


@dual_router.get("/journal/trades/{trade_id}")
def get_journal_trade(
    trade_id: str,
    store: JournalStore = Depends(get_journal_store),
) -> dict[str, Any]:
    """On dual_router (require_mobile_or_web) -- the web client's Journal detail view (M1) needs
    this."""
    trade = store.get_trade(trade_id)
    if trade is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "Journal trade not found")
    return trade


@dual_router.patch("/journal/trades/{trade_id}/notes")
def update_journal_notes(
    trade_id: str,
    body: JournalNotesRequest,
    store: JournalStore = Depends(get_journal_store),
) -> dict[str, Any]:
    """On dual_router (require_mobile_or_web) -- M5's seventh write endpoint exposed to the web
    client (Journal screen trade-notes editing, M5a)."""
    trade = store.save_notes(trade_id, body.model_dump())
    if trade is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "Journal trade not found")
    return trade


@protected_router.post("/journal/trades")
def create_manual_journal_trade(
    body: ManualJournalTradeRequest,
    store: JournalStore = Depends(get_journal_store),
) -> dict[str, Any]:
    payload = body.model_dump()
    if body.journal is not None:
        payload["journal"] = body.journal.model_dump()
    try:
        return store.create_manual_trade(payload)
    except DuplicateJournalTradeError as exc:
        raise _http_error(
            status.HTTP_409_CONFLICT,
            f"This trade is already in the journal ({exc.trade_id})",
        ) from exc


@dual_router.get("/analytics/summary")
def journal_analytics_summary(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    capital_base: Optional[float] = Query(default=None, gt=0),
    store: JournalStore = Depends(get_journal_store),
) -> dict[str, Any]:
    """On dual_router (require_mobile_or_web) -- the web client's Analytics screen (M1) needs
    this."""
    result = store.analytics_summary(start_date=start_date, end_date=end_date)
    result["net_pnl_percent"] = (
        result["net_pnl"] / capital_base * 100 if capital_base else None
    )
    return result


@protected_router.post("/journal/maintenance/correct-flat-brokerage")
def correct_flat_brokerage(store: JournalStore = Depends(get_journal_store)) -> dict[str, Any]:
    """One-off maintenance endpoint: corrects `computed_charges` recorded before
    UpstoxService.get_brokerage's flat-brokerage fix (30 -> 20/order). Trigger once by hand after
    deploying that fix; safe to call again (or leave in a cron/curl one-liner) -- a no-op past the
    first successful run. See JournalStore.backfill_flat_brokerage_correction's own doc comment.
    """
    return store.backfill_flat_brokerage_correction()


@protected_router.post("/notifications/read-all")
async def mark_all_notifications_read(
    store: NotificationStore = Depends(get_notification_store),
) -> dict[str, Any]:
    updated = store.mark_all_read()
    return {"updated": updated, "unread_count": store.unread_count()}


@protected_router.post("/notifications/register-device")
async def register_device(
    body: RegisterDeviceRequest,
    device_token_store: DeviceTokenStore = Depends(get_device_token_store),
) -> dict[str, Any]:
    """Registers this device's FCM push token and push-severity preference -- called on app
    start, on FCM token refresh, and whenever the user changes the preference in Settings. Always
    sends both together (see `DeviceTokenStore.save`'s own doc comment for why this always
    overwrites rather than merging)."""
    device_token_store.save(fcm_token=body.fcm_token, push_preference=body.push_preference)
    return {"status": "success"}


@dual_router.post("/orders/smart-bracket")
async def place_smart_bracket_order(
    order: SmartBracketOrderRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
    snapshot_store: SignalSnapshotStore = Depends(get_signal_snapshot_store),
    oi_snapshot_store: OISnapshotStore = Depends(get_oi_snapshot_store),
) -> dict[str, Any]:
    """Place a bracket-like order using Upstox multi-leg GTT.

    On dual_router (require_mobile_or_web) -- M3's first write endpoint exposed to the web client.
    Unlike every prior dual_router move (all read-only), this one places a real order; the web
    client's own confirmation dialog (always shown, no Android-style skip-confirmation mode) is
    the client-side safety gate, same posture Android's OrderConfirmationDialog already provides.
    """
    access_token = _load_access_token(token_store)
    try:
        rules = await InstrumentRulesService(settings).get_rules(order.instrument_key)
        validate_quantity(order.quantity, rules)
        validate_price(order.entry_trigger_price, rules, field_name="entry_trigger_price")
        validate_price(order.target_trigger_price, rules, field_name="target_trigger_price")
        validate_price(order.stoploss_trigger_price, rules, field_name="stoploss_trigger_price")
        slice_quantity = order.slice_quantity or slice_quantity_for_freeze(order.quantity, rules)
        result = await SmartOrderService(service).place_bracket_order(
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
        if order.underlying_key:
            order_ids = extract_order_ids(result)
            if order_ids:
                context_service = TradeContextService(
                    store=JournalStore(settings),
                    upstox=service,
                    signals=UnderlyingSignalsService(
                        service,
                        snapshot_store=snapshot_store,
                        oi_snapshot_store=oi_snapshot_store,
                    ),
                )
                asyncio.create_task(
                    context_service.capture(
                        access_token=access_token,
                        order_ids=order_ids,
                        trigger="placement",
                        instrument_key=order.instrument_key,
                        underlying_key=order.underlying_key,
                        expiry_date=order.signal_expiry_date,
                    )
                )
        return result
    except AppConfigError as exc:
        raise _http_error(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@dual_router.post("/orders/suggested-quantity")
def suggested_quantity(request: SuggestedQuantityRequest) -> dict[str, object]:
    """Server-side port of QuantitySizing.kt's defaultQuantity -- read-only computation, no side
    effects, safe on dual_router from the start (unlike smart-bracket, this never touches Upstox
    or persists anything)."""
    quantity = quantity_sizing.default_quantity(
        held_quantity=request.held_quantity,
        mode=request.mode,
        available_capital=request.available_capital,
        capital_allocation_percent=request.capital_allocation_percent,
        buffer_amount=request.buffer_amount,
        estimated_charges=request.estimated_charges,
        entry_price=request.entry_price,
        lot_size=request.lot_size,
        default_lot_count=request.default_lot_count,
        risk_per_trade_amount=request.risk_per_trade_amount,
        risk_management_is_percent=request.risk_management_is_percent,
        stop_loss_value=request.stop_loss_value,
        atr_14_5m=request.atr_14_5m,
        contract_delta=request.contract_delta,
        contract_iv=request.contract_iv,
        atr_stop_multiplier=request.atr_stop_multiplier,
        iv_stop_multiplier=request.iv_stop_multiplier,
        kelly_trade_count=request.kelly_trade_count,
        kelly_win_rate=request.kelly_win_rate,
        kelly_average_win=request.kelly_average_win,
        kelly_average_loss=request.kelly_average_loss,
        kelly_capital=request.kelly_capital,
    )
    return {"quantity": quantity}


@dual_router.get("/orders/gtt")
async def get_gtt_orders(
    instrument_key: Optional[str] = Query(None, min_length=1),
    include_history: bool = Query(False),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, Any]]:
    """Active GTT orders, optionally filtered to one instrument. The unfiltered form powers the
    Main screen's GTT Open Orders section; the filtered form lets the app find the bracket behind
    a position, or (with include_history=true) its historical bracket.
    See SmartOrderService.get_gtt_orders_for_instrument.

    Every order Upstox currently reports -- any status, any instrument -- is archived to
    GttHistoryStore on each call, not just the ones this response ends up returning. That's
    deliberate: the live response deliberately excludes terminal statuses (cancelled/rejected/
    completed) by default, and completed-request cleanup (see SmartOrderService's
    _cancel_stray_gtts) cancels brackets out from under a position the moment it's flattened -- an
    archive fed only from a filtered view would never observe either transition and would keep
    serving a stale pre-transition status forever.

    On dual_router (require_mobile_or_web) -- the web client's GTT screen (M3d) needs this.
    """
    access_token = _load_access_token(token_store)
    try:
        smart_order_service = SmartOrderService(service)
        all_orders = await smart_order_service.get_all_gtt_orders(access_token)
        history = GttHistoryStore(settings)
        history.archive(all_orders)
        if include_history:
            # Read back from the archive rather than all_orders -- durability is the point:
            # an order Upstox has since stopped listing entirely (aged out of its own API) still
            # needs to show up here if it was ever observed, which all_orders alone can't provide.
            return smart_order_service.filter_gtt_orders(
                history.list(instrument_key), include_history=True
            )
        return smart_order_service.filter_gtt_orders(
            all_orders, instrument_key=instrument_key, include_history=False
        )
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@dual_router.put("/orders/gtt/modify")
async def modify_gtt_order(
    order: ModifyGttOrderRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Re-points an existing GTT bracket's target/stoploss. See SmartOrderService.modify_gtt_bracket.

    On dual_router (require_mobile_or_web) -- M3's fourth write endpoint exposed to the web client.
    """
    access_token = _load_access_token(token_store)
    try:
        rules = await InstrumentRulesService(settings).get_rules(order.instrument_key)
        validate_quantity(order.quantity, rules)
        validate_price(order.entry_trigger_price, rules, field_name="entry_trigger_price")
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


@dual_router.delete("/orders/gtt/cancel")
async def cancel_gtt_order(
    order: CancelGttOrderRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Cancels an untriggered GTT order and all associated rules.

    On dual_router (require_mobile_or_web) -- M3's fifth write endpoint exposed to the web client.
    """
    access_token = _load_access_token(token_store)
    try:
        return await service.cancel_gtt_order(access_token, order.gtt_order_id)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@dual_router.get("/settings/max-loss")
def get_max_loss_settings(
    store: MaxLossSettingsStore = Depends(get_max_loss_settings_store),
) -> dict[str, float]:
    """Current max-loss threshold the backend's own max_loss_watcher enforces -- lets the app
    reconcile its local Order Settings value against the server on load (e.g. the watcher may
    have already fired and disarmed it while the app was closed).

    On dual_router (require_mobile_or_web) -- the web client's Settings screen (M3e) reads this.
    """
    return {"amount": store.load()}


@dual_router.put("/settings/max-loss")
def set_max_loss_settings(
    request: MaxLossSettingsRequest,
    store: MaxLossSettingsStore = Depends(get_max_loss_settings_store),
) -> dict[str, float]:
    """Sets the max-loss threshold the backend's own max_loss_watcher enforces -- called whenever
    the user edits the amount in Order Settings, so the watcher stays in sync with whatever the
    app itself is configured to protect against. `amount <= 0` disables it.

    On dual_router (require_mobile_or_web) -- M3's sixth write endpoint exposed to the web client.
    """
    store.save(request.amount)
    return {"amount": request.amount}


@protected_router.post("/orders/exit-all")
async def exit_all_positions(
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
    notification_service: NotificationService = Depends(get_notification_service),
    exit_all_lock: asyncio.Lock = Depends(get_exit_all_lock),
) -> dict[str, Any]:
    """Flattens every currently open position with an immediate market order -- backs the app's
    own max-loss auto square-off (MainViewModel.checkMaxLoss). See
    SmartOrderService.exit_all_positions.

    Held under [exit_all_lock] -- shared with the backend's own max_loss_watcher, which can
    trigger the exact same flatten independently (e.g. the app is closed). Without this, a
    client-triggered flatten and the watcher's own could race: Upstox's position book doesn't
    always reflect a just-placed market order's fill instantly, so both could see the same
    position as still open and each submit their own exit, flattening it twice -- e.g. closing a
    long with two separate sell orders leaves a net *short* position instead of flat.
    """
    access_token = _load_access_token(token_store)
    async with exit_all_lock:
        try:
            result = await SmartOrderService(service).exit_all_positions(
                access_token,
                instrument_rules_service=InstrumentRulesService(settings),
            )
        except UpstoxApiError as exc:
            raise _upstox_http_error(exc) from exc
    await _notify_if_exit_had_failures(notification_service, result)
    return result


@dual_router.post("/orders/exit-positions")
async def exit_positions(
    request: ExitPositionsRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
    notification_service: NotificationService = Depends(get_notification_service),
    exit_all_lock: asyncio.Lock = Depends(get_exit_all_lock),
) -> dict[str, Any]:
    """Flattens open positions with an immediate market order, optionally scoped to
    [ExitPositionsRequest.instrument_keys] (e.g. "close only profitable positions", computed
    client-side). See SmartOrderService.exit_positions and [exit_all_positions]'s own doc
    comment for why this shares the same lock.

    On dual_router (require_mobile_or_web) -- M3's second write endpoint exposed to the web
    client, after /orders/smart-bracket (M3a). /orders/exit-all stays on protected_router,
    untouched -- the web client always calls this route with no instrument_keys for "close
    everything," identical behavior, so there's no need to expose that one too.
    """
    access_token = _load_access_token(token_store)
    async with exit_all_lock:
        try:
            result = await SmartOrderService(service).exit_positions(
                access_token,
                instrument_keys=request.instrument_keys,
                instrument_rules_service=InstrumentRulesService(settings),
            )
        except UpstoxApiError as exc:
            raise _upstox_http_error(exc) from exc
    await _notify_if_exit_had_failures(notification_service, result)
    return result


async def _notify_if_exit_had_failures(
    notification_service: NotificationService, result: dict[str, Any],
) -> None:
    """Records a `risk`-category notification when any position failed to flatten after
    SmartOrderService.exit_positions's own retry loop gave up -- covers both the "max-loss result
    had failures" and "exit retries exhausted" scenarios in one message, since a result reaching
    here with status="error" for a position *is* exactly a retry-exhausted outcome."""
    failed = [item for item in result.get("results", []) if item.get("status") == "error"]
    if not failed:
        return
    await notification_service.record(
        category="risk",
        severity="critical",
        title="Position exit failed",
        message=f"{len(failed)} of {result.get('positions_found', len(failed))} position(s) could not be flattened.",
        details={"positions_found": result.get("positions_found"), "results": result.get("results")},
    )


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


@protected_router.post("/orders/cancel")
async def cancel_orders(
    request: CancelOrdersRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Cancel any number of still-open regular orders. See
    OrderCancellationService.cancel_orders.
    """
    access_token = _load_access_token(token_store)
    return await OrderCancellationService(service).cancel_orders(access_token, request.order_ids)


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
router.include_router(web_router)
router.include_router(dual_router)
