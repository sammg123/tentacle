"""AlpacaBroker: a real REST adapter over the Alpaca trading API.

No SDK: we talk to the REST endpoints directly with the shared ``http_*`` helpers
plus a couple of guarded ``requests`` POST/DELETE calls. The trade base comes from
cfg.integrations.alpaca_trade_base (paper by default). Auth is the standard
APCA-API-KEY-ID / APCA-API-SECRET-KEY header pair. Market data (price + clock) is
delegated to marketdata so we use one consistent data path.

LONG ONLY: submit_buy places a market (or limit) day order on the buy side;
close_position flattens via the dedicated /v2/positions/{symbol} DELETE. There is
no short, no margin, no options, no crypto here.

Everything is guarded. Network or parse failures degrade to None / "error" status
so a venue hiccup never crashes the loop or books a phantom fill.
"""
from __future__ import annotations

from typing import Optional

from ..config import Config, get_config
from ..storage import Storage, get_storage
from ..util import default_user_agent, get_logger, http_json, now_iso, stable_id
from .base import Broker, BrokerOrder

log = get_logger("tentacle.broker.alpaca")


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, cfg: Optional[Config] = None, storage: Optional[Storage] = None,
                 md=None):
        self.cfg = cfg or get_config()
        self.storage = storage or get_storage()
        if md is None:
            from ..marketdata import get_marketdata
            md = get_marketdata()
        self.md = md
        integ = self.cfg.integrations
        self.key = integ.alpaca_key
        self.secret = integ.alpaca_secret
        self.base = integ.alpaca_trade_base
        self.headers = {
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "User-Agent": default_user_agent(),
            "accept": "application/json",
        }

    @classmethod
    def has_keys(cls, cfg: Optional[Config] = None) -> bool:
        cfg = cfg or get_config()
        try:
            i = cfg.integrations
            return bool(i.alpaca_key and i.alpaca_secret)
        except Exception:
            return False

    # ------------------------------------------------------------------ clock
    def is_market_open(self) -> bool:
        # Delegate to marketdata (which itself prefers the Alpaca clock when keyed).
        try:
            return bool(self.md.is_market_open())
        except Exception:
            return False

    # ------------------------------------------------------------------ price
    def last_price(self, ticker: str) -> Optional[float]:
        try:
            px = self.md.last_price(ticker)
            return float(px) if px is not None else None
        except Exception:
            return None

    # ------------------------------------------------------------------ account
    def get_account(self) -> dict:
        data = http_json(f"{self.base}/v2/account", headers=self.headers)
        if not isinstance(data, dict):
            return {"equity": 0.0, "cash": 0.0, "settled_cash": 0.0,
                    "unsettled_cash": 0.0, "buying_power": 0.0}

        def _g(name: str) -> float:
            try:
                return float(data.get(name) or 0.0)
            except Exception:
                return 0.0

        cash = _g("cash")
        equity = _g("equity") or _g("portfolio_value")
        buying_power = _g("buying_power")
        # Alpaca does not expose a clean settled/unsettled split for cash accounts
        # in the account object. pending_transfer_in is the closest proxy for
        # incoming-but-not-yet-usable funds, so treat it as unsettled and the rest
        # of cash as settled. Documented approximation.
        pending_in = _g("pending_transfer_in")
        unsettled = pending_in
        settled = max(0.0, cash - unsettled)
        return {
            "equity": equity,
            "cash": cash,
            "settled_cash": settled,
            "unsettled_cash": unsettled,
            "buying_power": buying_power,
        }

    # ------------------------------------------------------------------ positions
    def list_positions(self) -> list[dict]:
        data = http_json(f"{self.base}/v2/positions", headers=self.headers)
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for p in data:
            if not isinstance(p, dict):
                continue
            norm = self._normalize_position(p)
            if norm is not None:
                out.append(norm)
        return out

    def get_position(self, ticker: str) -> Optional[dict]:
        ticker = (ticker or "").upper().strip()
        if not ticker:
            return None
        data = http_json(f"{self.base}/v2/positions/{ticker}", headers=self.headers)
        if not isinstance(data, dict) or "symbol" not in data:
            return None
        return self._normalize_position(data)

    def _normalize_position(self, p: dict) -> Optional[dict]:
        try:
            ticker = str(p.get("symbol") or "").upper().strip()
            if not ticker:
                return None

            def _g(name: str) -> float:
                try:
                    return float(p.get(name) or 0.0)
                except Exception:
                    return 0.0

            qty = _g("qty")
            # LONG ONLY: ignore any short rows defensively (should never exist).
            if qty <= 0:
                return None
            return {
                "ticker": ticker,
                "qty": qty,
                "avg_entry_price": _g("avg_entry_price"),
                "current_price": _g("current_price"),
                "market_value": _g("market_value"),
                "cost_basis": _g("cost_basis"),
                "unrealized_pl": _g("unrealized_pl"),
            }
        except Exception:
            return None

    # ------------------------------------------------------------------ orders
    def submit_buy(self, ticker: str, qty: float, *,
                   limit: Optional[float] = None) -> BrokerOrder:
        ticker = (ticker or "").upper().strip()
        try:
            qty = float(qty)
        except Exception:
            qty = 0.0
        if not ticker or qty <= 0:
            return self._error(ticker, qty, "buy", "bad ticker/qty")
        if not self.has_keys(self.cfg):
            return self._error(ticker, qty, "buy", "no alpaca keys")

        body: dict = {
            "symbol": ticker,
            "side": "buy",
            "type": "limit" if limit is not None else "market",
            "time_in_force": "day",
        }
        if limit is not None:
            try:
                body["limit_price"] = round(float(limit), 4)
            except Exception:
                body["type"] = "market"
                body.pop("limit_price", None)

        # Fractional support: if the notional is small, route as a notional order so
        # Alpaca can fill fractional shares (market only). Otherwise send whole-ish
        # qty. We keep it simple and honest: whole-share market/limit by default.
        px = self.last_price(ticker)
        if (limit is None and px is not None and qty < 1.0 and qty > 0):
            # Sub-one-share: use notional (market only).
            body.pop("type", None)
            body["type"] = "market"
            body["notional"] = round(qty * px, 2)
            body.pop("qty", None)
        else:
            body["qty"] = str(qty)

        resp = self._post("/v2/orders", body)
        if not isinstance(resp, dict) or resp.get("id") is None:
            return self._error(ticker, qty, "buy",
                               f"order rejected: {self._err_text(resp)}")

        return self._order_from_alpaca(resp, fallback_ticker=ticker,
                                       fallback_qty=qty, side="buy")

    def close_position(self, ticker: str) -> BrokerOrder:
        ticker = (ticker or "").upper().strip()
        if not ticker:
            return self._error(ticker, 0.0, "sell", "bad ticker")
        if not self.has_keys(self.cfg):
            return self._error(ticker, 0.0, "sell", "no alpaca keys")

        resp = self._delete(f"/v2/positions/{ticker}")
        if not isinstance(resp, dict) or resp.get("id") is None:
            return self._error(ticker, 0.0, "sell",
                               f"close rejected: {self._err_text(resp)}")
        return self._order_from_alpaca(resp, fallback_ticker=ticker,
                                       fallback_qty=0.0, side="sell")

    # ------------------------------------------------------------------ http
    def _post(self, path: str, body: dict) -> Optional[dict]:
        try:
            import requests
        except Exception:
            return None
        try:
            resp = requests.post(f"{self.base}{path}", json=body,
                                 headers=self.headers, timeout=20)
            try:
                return resp.json()
            except Exception:
                return {"_status": resp.status_code, "_text": resp.text}
        except Exception as exc:  # noqa: BLE001
            log.debug("alpaca POST %s failed: %s", path, exc)
            return None

    def _delete(self, path: str) -> Optional[dict]:
        try:
            import requests
        except Exception:
            return None
        try:
            resp = requests.delete(f"{self.base}{path}", headers=self.headers, timeout=20)
            try:
                return resp.json()
            except Exception:
                return {"_status": resp.status_code, "_text": resp.text}
        except Exception as exc:  # noqa: BLE001
            log.debug("alpaca DELETE %s failed: %s", path, exc)
            return None

    # ------------------------------------------------------------------ helpers
    def _order_from_alpaca(self, o: dict, *, fallback_ticker: str,
                           fallback_qty: float, side: str) -> BrokerOrder:
        try:
            oid = str(o.get("id") or "")
            ticker = str(o.get("symbol") or fallback_ticker).upper().strip()

            def _g(name: str, default: float = 0.0) -> float:
                try:
                    v = o.get(name)
                    return float(v) if v is not None else default
                except Exception:
                    return default

            qty = _g("filled_qty") or _g("qty") or fallback_qty
            filled_price = _g("filled_avg_price")
            status = str(o.get("status") or "accepted")
            # A freshly accepted market order often has no fill price yet. Mark it
            # "accepted" (not "filled") so the engine does not book it until it is
            # confirmed filled. filled() in base treats only filled states as real.
            if filled_price <= 0 and status not in {"filled", "partially_filled"}:
                # leave price 0 and the non-filled status; caller can poll later.
                pass
            return BrokerOrder(
                order_id=oid, ticker=ticker, qty=qty, side=side,
                filled_price=filled_price, status=status, raw=o,
            )
        except Exception:
            return self._error(fallback_ticker, fallback_qty, side, "parse error")

    @staticmethod
    def _err_text(resp) -> str:
        if isinstance(resp, dict):
            return str(resp.get("message") or resp.get("_text") or resp)
        return str(resp)

    def _error(self, ticker: str, qty: float, side: str, reason: str) -> BrokerOrder:
        log.debug("alpaca %s error for %s: %s", side, ticker, reason)
        return BrokerOrder(
            order_id="", ticker=(ticker or "").upper(), qty=qty, side=side,
            filled_price=0.0, status="error", raw={"reason": reason},
        )
