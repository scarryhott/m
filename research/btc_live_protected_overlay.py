#!/usr/bin/env python3
"""Live paper test for a protected BTC core plus disposable leverage overlay.

Public data only. No credentials and no real orders.
Primary source: Kraken BTC/USD PostTrade and PreTrade endpoints.
"""
from __future__ import annotations

import csv
import json
import math
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

POSTTRADE_URL = "https://api.kraken.com/0/public/PostTrade"
PRETRADE_URL = "https://api.kraken.com/0/public/PreTrade"
CLASSIC_TRADES_URL = "https://api.kraken.com/0/public/Trades"
SYMBOL = "BTC/USD"
PAIR = "XBTUSD"
USER_AGENT = "btc-protected-core-overlay-paper/1.0"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_ms(value: str) -> int:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, tail = text.split(".", 1)
        plus = tail.find("+")
        minus = tail.find("-")
        positions = [x for x in (plus, minus) if x >= 0]
        zone_index = min(positions) if positions else -1
        if zone_index >= 0:
            frac, zone = tail[:zone_index], tail[zone_index:]
        else:
            frac, zone = tail, ""
        text = f"{head}.{frac[:6].ljust(6, '0')}{zone}"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def http_json(url: str, params: dict[str, Any], retries: int = 4) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}" if query else url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise ValueError("API response was not an object")
            errors = payload.get("error")
            if isinstance(errors, list) and errors:
                raise RuntimeError(f"Kraken API error: {errors}")
            return payload
        except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(0.5 * (2**attempt))
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


@dataclass(frozen=True, slots=True)
class Tick:
    ts_ms: int
    price: float
    quantity: float
    trade_id: str
    side: str


@dataclass(frozen=True, slots=True)
class Book:
    ts_ms: int
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float


@dataclass(frozen=True, slots=True)
class Config:
    closure_basis_usd: float = 1171.3682902268508
    protected_core_usd: float = 1000.0
    overlay_seed_usd: float = 171.3682902268508
    fee_rate: float = 0.0004
    maintenance_margin_rate: float = 0.005
    liquidation_fee_rate: float = 0.002
    max_overlay_leverage: float = 5.0
    min_overlay_leverage: float = 1.25
    loss_budget_fraction: float = 0.10
    liquidation_reserve_fraction: float = 0.20
    fast_ticks: int = 20
    slow_ticks: int = 80
    volatility_ticks: int = 200
    volatility_horizon_ticks: int = 30
    volatility_multiplier: float = 3.0
    minimum_adverse_move: float = 0.0015
    minimum_breakout: float = 0.0005
    breakout_volatility_multiplier: float = 1.5
    entry_confirmation_ticks: int = 8
    exit_confirmation_ticks: int = 5
    overlay_peak_giveback: float = 0.20
    maximum_position_seconds: int = 180
    strict_harvest_threshold: float = 0.002
    minimum_overlay_cash: float = 5.0


@dataclass(slots=True)
class Signal:
    fast: float
    slow: float
    prev_fast: float
    prev_price: float
    returns: deque[float]
    up_count: int = 0
    down_count: int = 0

    @classmethod
    def create(cls, price: float, config: Config) -> "Signal":
        return cls(price, price, price, price, deque(maxlen=config.volatility_ticks))

    def update(self, price: float, config: Config) -> None:
        self.prev_fast = self.fast
        if price > 0 and self.prev_price > 0:
            self.returns.append(math.log(price / self.prev_price))
        self.prev_price = price
        alpha_fast = 2.0 / (config.fast_ticks + 1.0)
        alpha_slow = 2.0 / (config.slow_ticks + 1.0)
        self.fast = alpha_fast * price + (1.0 - alpha_fast) * self.fast
        self.slow = alpha_slow * price + (1.0 - alpha_slow) * self.slow
        up = self.fast > self.slow and self.fast >= self.prev_fast
        down = self.fast < self.slow and self.fast <= self.prev_fast
        self.up_count = self.up_count + 1 if up else 0
        self.down_count = self.down_count + 1 if down else 0

    def sigma(self) -> float:
        return statistics.pstdev(self.returns) if len(self.returns) >= 10 else 0.0


@dataclass(slots=True)
class Overlay:
    cash: float
    seed: float
    qty: float = 0.0
    entry_price: float = 0.0
    entry_time_ms: int = 0
    entry_notional: float = 0.0
    peak_equity: float = 0.0
    fees: float = 0.0
    liquidated: bool = False

    def is_open(self) -> bool:
        return self.qty > 0.0

    def equity(self, mark: float) -> float:
        if not self.is_open():
            return self.cash
        return self.cash + self.qty * (mark - self.entry_price)

    def notional(self, mark: float) -> float:
        return self.qty * mark

    def open_long(self, ask: float, target_leverage: float, fee_rate: float, ts_ms: int) -> dict[str, float]:
        if self.is_open():
            raise RuntimeError("overlay already open")
        notional = target_leverage * self.cash / (1.0 + target_leverage * fee_rate)
        fee = notional * fee_rate
        self.cash -= fee
        self.qty = notional / ask
        self.entry_price = ask
        self.entry_time_ms = ts_ms
        self.entry_notional = notional
        self.peak_equity = self.cash
        self.fees += fee
        return {"notional": notional, "fee": fee, "quantity": self.qty}

    def close_long(self, bid: float, fee_rate: float) -> dict[str, float]:
        if not self.is_open():
            return {"pnl": 0.0, "fee": 0.0, "equity_after": self.cash}
        pnl = self.qty * (bid - self.entry_price)
        fee = self.qty * bid * fee_rate
        self.cash += pnl - fee
        self.fees += fee
        self.qty = 0.0
        self.entry_price = 0.0
        self.entry_time_ms = 0
        self.entry_notional = 0.0
        self.peak_equity = self.cash
        return {"pnl": pnl, "fee": fee, "equity_after": self.cash}

    def liquidate(self, mark: float, config: Config) -> dict[str, float]:
        notional = self.notional(mark)
        before = self.equity(mark)
        fee = notional * config.liquidation_fee_rate
        remaining = max(0.0, before - notional * config.maintenance_margin_rate - fee)
        self.cash = remaining
        self.qty = 0.0
        self.entry_price = 0.0
        self.entry_time_ms = 0
        self.entry_notional = 0.0
        self.peak_equity = remaining
        self.fees += fee
        self.liquidated = True
        return {"equity_before": before, "fee": fee, "equity_after": remaining}


@dataclass(slots=True)
class ProtectedSystem:
    name: str
    config: Config
    core_qty: float
    core_floor_usd: float
    core_reserve_usd: float
    overlay: Overlay
    basis_price: float
    basis_equity_usd: float
    signal: Signal
    events: list[dict[str, Any]]
    max_total_equity: float
    max_drawdown: float = 0.0

    @classmethod
    def split_basis(cls, price: float, config: Config) -> "ProtectedSystem":
        core_qty = config.protected_core_usd / price
        overlay = Overlay(config.overlay_seed_usd, config.overlay_seed_usd)
        total = config.protected_core_usd + config.overlay_seed_usd
        return cls(
            "split_core_plus_disposable_overlay", config, core_qty,
            config.protected_core_usd, 0.0, overlay, price, total,
            Signal.create(price, config), [], total,
        )

    @classmethod
    def strict_full_basis(cls, price: float, config: Config) -> "ProtectedSystem":
        core_qty = config.closure_basis_usd / price
        overlay = Overlay(0.0, 0.0)
        return cls(
            "strict_full_basis_no_seed_overlay", config, core_qty,
            config.closure_basis_usd, 0.0, overlay, price,
            config.closure_basis_usd, Signal.create(price, config), [],
            config.closure_basis_usd,
        )

    def core_marked(self, mark: float) -> float:
        return self.core_qty * mark + self.core_reserve_usd

    def total_equity(self, mark: float) -> float:
        return self.core_marked(mark) + self.overlay.equity(mark)

    def protected_ledger(self) -> float:
        return self.core_floor_usd + self.core_reserve_usd

    def update_drawdown(self, mark: float) -> None:
        equity = self.total_equity(mark)
        self.max_total_equity = max(self.max_total_equity, equity)
        if self.max_total_equity > 0:
            self.max_drawdown = max(self.max_drawdown, (self.max_total_equity - equity) / self.max_total_equity)

    def maybe_harvest_strict_core(self, bid: float, ts_ms: int) -> None:
        if self.overlay.seed > 0 or self.overlay.is_open():
            return
        marked_spot = self.core_qty * bid
        threshold = self.core_floor_usd * (1.0 + self.config.strict_harvest_threshold)
        if marked_spot <= threshold:
            return
        harvest = marked_spot - self.core_floor_usd
        sell_qty = harvest / bid
        self.core_qty -= sell_qty
        self.overlay.cash += harvest
        self.overlay.seed += harvest
        self.events.append({
            "type": "core_gain_harvested_to_overlay", "time": iso_ms(ts_ms),
            "price": bid, "harvest_usd": harvest,
            "protected_ledger_after": self.protected_ledger(),
            "core_spot_mark_after": self.core_qty * bid,
        })

    def target_leverage(self) -> dict[str, float]:
        sigma = self.signal.sigma()
        adverse = max(
            self.config.minimum_adverse_move,
            self.config.volatility_multiplier * sigma * math.sqrt(self.config.volatility_horizon_ticks),
        )
        risk_cap = self.config.loss_budget_fraction / adverse
        liquidation_cap = (1.0 - self.config.liquidation_reserve_fraction) / (
            adverse + self.config.maintenance_margin_rate + self.config.fee_rate
        )
        target = min(self.config.max_overlay_leverage, risk_cap, liquidation_cap)
        return {
            "target": max(1.0, target), "sigma": sigma,
            "adverse_move": adverse, "risk_cap": risk_cap,
            "liquidation_cap": liquidation_cap,
        }

    def on_tick(self, tick: Tick, book: Book) -> None:
        mark = (book.bid + book.ask) / 2.0
        self.signal.update(tick.price, self.config)
        self.maybe_harvest_strict_core(book.bid, tick.ts_ms)
        self.update_drawdown(mark)

        if self.overlay.is_open():
            overlay_equity = self.overlay.equity(mark)
            maintenance = self.overlay.notional(mark) * self.config.maintenance_margin_rate
            if overlay_equity <= maintenance:
                result = self.overlay.liquidate(mark, self.config)
                self.events.append({
                    "type": "overlay_liquidated_core_untouched",
                    "time": iso_ms(tick.ts_ms), "mark": mark,
                    "core_qty": self.core_qty,
                    "protected_ledger": self.protected_ledger(), **result,
                })
                return

            self.overlay.peak_equity = max(self.overlay.peak_equity, overlay_equity)
            age_seconds = (tick.ts_ms - self.overlay.entry_time_ms) / 1000.0
            adverse_info = self.target_leverage()
            stop_price = self.overlay.entry_price * (1.0 - adverse_info["adverse_move"])
            giveback_floor = self.overlay.peak_equity * (1.0 - self.config.overlay_peak_giveback)
            reason = None
            if self.signal.down_count >= self.config.exit_confirmation_ticks:
                reason = "confirmed_downtrend"
            elif overlay_equity <= giveback_floor:
                reason = "overlay_peak_giveback"
            elif mark <= stop_price:
                reason = "volatility_stop"
            elif age_seconds >= self.config.maximum_position_seconds:
                reason = "maximum_position_age"
            if reason:
                before = overlay_equity
                result = self.overlay.close_long(book.bid, self.config.fee_rate)
                transfer = max(0.0, self.overlay.cash - self.overlay.seed)
                if transfer > 0:
                    self.overlay.cash -= transfer
                    self.core_reserve_usd += transfer
                    self.basis_equity_usd += transfer
                self.basis_price = book.bid
                self.events.append({
                    "type": "overlay_closed_new_protected_basis",
                    "time": iso_ms(tick.ts_ms), "price": book.bid,
                    "reason": reason, "overlay_equity_before": before,
                    "profit_transferred_to_core": transfer,
                    "protected_ledger_after": self.protected_ledger(),
                    "overlay_cash_after": self.overlay.cash, **result,
                })
            return

        if self.overlay.cash < self.config.minimum_overlay_cash:
            return
        if len(self.signal.returns) < self.config.slow_ticks:
            return
        sigma = self.signal.sigma()
        breakout = max(
            self.config.minimum_breakout,
            self.config.breakout_volatility_multiplier * sigma * math.sqrt(self.config.volatility_horizon_ticks),
        )
        breakout_price = self.basis_price * (1.0 + breakout)
        if self.signal.up_count < self.config.entry_confirmation_ticks or book.ask < breakout_price:
            return
        leverage_info = self.target_leverage()
        target = leverage_info["target"]
        if target < self.config.min_overlay_leverage:
            return
        result = self.overlay.open_long(book.ask, target, self.config.fee_rate, tick.ts_ms)
        self.events.append({
            "type": "overlay_opened_from_disposable_surplus",
            "time": iso_ms(tick.ts_ms), "price": book.ask,
            "basis_price": self.basis_price,
            "protected_ledger": self.protected_ledger(),
            "target_leverage": target, "breakout_price": breakout_price,
            **leverage_info, **result,
        })

    def finish(self, last_book: Book) -> dict[str, Any]:
        if self.overlay.is_open():
            before = self.overlay.equity(last_book.bid)
            result = self.overlay.close_long(last_book.bid, self.config.fee_rate)
            transfer = max(0.0, self.overlay.cash - self.overlay.seed)
            if transfer > 0:
                self.overlay.cash -= transfer
                self.core_reserve_usd += transfer
                self.basis_equity_usd += transfer
            self.events.append({
                "type": "overlay_closed_at_test_end",
                "time": iso_ms(last_book.ts_ms), "price": last_book.bid,
                "overlay_equity_before": before,
                "profit_transferred_to_core": transfer, **result,
            })
        mark = (last_book.bid + last_book.ask) / 2.0
        final_equity = self.total_equity(mark)
        return {
            "name": self.name, "final_total_equity": final_equity,
            "return_on_closure_basis_pct": (final_equity / self.config.closure_basis_usd - 1.0) * 100.0,
            "core_marked_equity": self.core_marked(mark),
            "protected_ledger": self.protected_ledger(),
            "core_quantity_btc": self.core_qty,
            "overlay_cash": self.overlay.cash,
            "overlay_seed": self.overlay.seed,
            "overlay_fees": self.overlay.fees,
            "overlay_liquidated": self.overlay.liquidated,
            "maximum_drawdown_pct": self.max_drawdown * 100.0,
            "events": self.events,
            "event_counts": {
                "opens": sum(e["type"] == "overlay_opened_from_disposable_surplus" for e in self.events),
                "closes": sum(e["type"].startswith("overlay_closed") for e in self.events),
                "liquidations": sum(e["type"] == "overlay_liquidated_core_untouched" for e in self.events),
                "harvests": sum(e["type"] == "core_gain_harvested_to_overlay" for e in self.events),
            },
        }


def normalize_posttrade(payload: dict[str, Any]) -> tuple[list[Tick], str | None]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return [], None
    raw = result.get("trades")
    if not isinstance(raw, list):
        return [], None
    ticks: list[Tick] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            ticks.append(Tick(parse_iso_ms(str(item["trade_ts"])), float(item["price"]),
                              float(item["quantity"]), str(item["trade_id"]),
                              str(item.get("side", ""))))
        except (KeyError, TypeError, ValueError):
            continue
    cursor = str(result.get("last_ts")) if result.get("last_ts") else None
    return ticks, cursor


def normalize_classic(payload: dict[str, Any]) -> tuple[list[Tick], str | None]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return [], None
    pair_key = next((key for key in result if key != "last"), None)
    raw = result.get(pair_key, []) if pair_key else []
    ticks: list[Tick] = []
    for index, row in enumerate(raw):
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            ts_ms = int(float(row[2]) * 1000)
            trade_id = str(row[6]) if len(row) > 6 else f"{ts_ms}:{index}:{row[0]}:{row[1]}"
            ticks.append(Tick(ts_ms, float(row[0]), float(row[1]), trade_id, str(row[3])))
        except (TypeError, ValueError):
            continue
    return ticks, str(result.get("last")) if result.get("last") else None


def normalize_book(payload: dict[str, Any], fallback_price: float) -> Book:
    result = payload.get("result")
    ts_ms = int(time.time() * 1000)
    if isinstance(result, dict):
        bids = result.get("bids")
        asks = result.get("asks")
        try:
            bid_item = bids[0]
            ask_item = asks[0]
            if isinstance(bid_item, dict):
                bid, bid_qty = float(bid_item["price"]), float(bid_item.get("qty", 0.0))
            else:
                bid, bid_qty = float(bid_item[0]), float(bid_item[1])
            if isinstance(ask_item, dict):
                ask, ask_qty = float(ask_item["price"]), float(ask_item.get("qty", 0.0))
            else:
                ask, ask_qty = float(ask_item[0]), float(ask_item[1])
            return Book(ts_ms, bid, ask, bid_qty, ask_qty)
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    half_spread = fallback_price * 0.00005
    return Book(ts_ms, fallback_price - half_spread, fallback_price + half_spread, 0.0, 0.0)


def capture(seconds: int, poll_seconds: float = 1.0) -> tuple[list[Tick], list[Book], dict[str, Any]]:
    started = utc_now_iso()
    mode = "posttrade"
    cursor: str | None = None
    all_ticks: dict[str, Tick] = {}
    books: list[Book] = []
    errors: list[str] = []
    try:
        payload = http_json(POSTTRADE_URL, {"symbol": SYMBOL, "count": 1000})
        initial, cursor = normalize_posttrade(payload)
        if not initial:
            raise RuntimeError("PostTrade returned no ticks")
    except Exception as exc:
        mode = "classic"
        errors.append(f"posttrade fallback: {type(exc).__name__}: {exc}")
        payload = http_json(CLASSIC_TRADES_URL, {"pair": PAIR})
        initial, cursor = normalize_classic(payload)
    for tick in initial:
        all_ticks[tick.trade_id] = tick
    last_price = initial[-1].price
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        cycle_start = time.monotonic()
        try:
            if mode == "posttrade":
                params: dict[str, Any] = {"symbol": SYMBOL, "count": 1000}
                if cursor:
                    params["from_ts"] = cursor
                payload = http_json(POSTTRADE_URL, params)
                ticks, next_cursor = normalize_posttrade(payload)
            else:
                params = {"pair": PAIR}
                if cursor:
                    params["since"] = cursor
                payload = http_json(CLASSIC_TRADES_URL, params)
                ticks, next_cursor = normalize_classic(payload)
            if next_cursor:
                cursor = next_cursor
            for tick in ticks:
                all_ticks[tick.trade_id] = tick
                last_price = tick.price
        except Exception as exc:
            errors.append(f"trade poll: {type(exc).__name__}: {exc}")
        try:
            book_payload = http_json(PRETRADE_URL, {"symbol": SYMBOL})
            books.append(normalize_book(book_payload, last_price))
        except Exception as exc:
            errors.append(f"book poll: {type(exc).__name__}: {exc}")
            half_spread = last_price * 0.00005
            books.append(Book(int(time.time() * 1000), last_price - half_spread,
                              last_price + half_spread, 0.0, 0.0))
        remaining = poll_seconds - (time.monotonic() - cycle_start)
        if remaining > 0:
            time.sleep(remaining)
    ticks = sorted(all_ticks.values(), key=lambda item: (item.ts_ms, item.trade_id))
    books.sort(key=lambda item: item.ts_ms)
    metadata = {
        "source": "kraken_btc_usd_posttrade_pretrade" if mode == "posttrade" else "kraken_classic_trades_with_pretrade",
        "capture_started_at": started, "capture_finished_at": utc_now_iso(),
        "capture_seconds": seconds, "poll_seconds": poll_seconds,
        "unique_trade_count": len(ticks), "book_snapshot_count": len(books),
        "errors": errors,
    }
    return ticks, books, metadata


def books_for_ticks(ticks: list[Tick], books: list[Book]) -> list[Book]:
    if not books:
        return [Book(t.ts_ms, t.price * 0.99995, t.price * 1.00005, 0.0, 0.0) for t in ticks]
    aligned: list[Book] = []
    index = 0
    current = books[0]
    for tick in ticks:
        while index + 1 < len(books) and books[index + 1].ts_ms <= tick.ts_ms:
            index += 1
            current = books[index]
        aligned.append(current)
    return aligned


def run_test(ticks: list[Tick], books: list[Book], config: Config) -> dict[str, Any]:
    if len(ticks) < 2:
        raise RuntimeError(f"need at least two ticks, found {len(ticks)}")
    aligned = books_for_ticks(ticks, books)
    first_mark = (aligned[0].bid + aligned[0].ask) / 2.0
    systems = [ProtectedSystem.split_basis(first_mark, config),
               ProtectedSystem.strict_full_basis(first_mark, config)]
    for tick, book in zip(ticks[1:], aligned[1:]):
        for system in systems:
            system.on_tick(tick, book)
    results = [system.finish(aligned[-1]) for system in systems]
    return {
        "paper_only": True,
        "architecture": {
            "closure_basis_usd": config.closure_basis_usd,
            "split_variant": {"protected_core_usd": config.protected_core_usd,
                              "disposable_overlay_seed_usd": config.overlay_seed_usd},
            "strict_variant": {"protected_core_usd": config.closure_basis_usd,
                               "disposable_overlay_seed_usd": 0.0},
            "invariant": "overlay losses and liquidation cannot debit or liquidate the protected core ledger",
        },
        "market": {
            "tick_count": len(ticks), "first_time": iso_ms(ticks[0].ts_ms),
            "last_time": iso_ms(ticks[-1].ts_ms), "first_price": ticks[0].price,
            "last_price": ticks[-1].price, "high_price": max(t.price for t in ticks),
            "low_price": min(t.price for t in ticks),
            "price_change_pct": (ticks[-1].price / ticks[0].price - 1.0) * 100.0,
            "volume_btc": sum(t.quantity for t in ticks),
        },
        "results": results, "config": asdict(config),
    }


def write_outputs(ticks: list[Tick], books: list[Book], metadata: dict[str, Any], result: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with (output / "live_trades.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ts_ms", "time", "price", "quantity", "trade_id", "side"])
        writer.writeheader()
        for tick in ticks:
            writer.writerow({"ts_ms": tick.ts_ms, "time": iso_ms(tick.ts_ms),
                             "price": tick.price, "quantity": tick.quantity,
                             "trade_id": tick.trade_id, "side": tick.side})
    with (output / "live_books.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ts_ms", "time", "bid", "ask", "bid_qty", "ask_qty"])
        writer.writeheader()
        for book in books:
            writer.writerow({"ts_ms": book.ts_ms, "time": iso_ms(book.ts_ms),
                             "bid": book.bid, "ask": book.ask,
                             "bid_qty": book.bid_qty, "ask_qty": book.ask_qty})
    payload = {"capture": metadata, **result}
    (output / "live_core_overlay_result.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def synthetic_self_test() -> dict[str, Any]:
    config = Config(fast_ticks=3, slow_ticks=5, volatility_ticks=8,
                    volatility_horizon_ticks=2, entry_confirmation_ticks=2,
                    exit_confirmation_ticks=2, minimum_breakout=0.0001,
                    minimum_adverse_move=0.002, maximum_position_seconds=1000)
    prices = [100.0, 100.01, 100.03, 100.08, 100.15, 100.25, 100.35,
              100.45, 100.40, 100.30, 100.20]
    ticks = [Tick(i * 1000, price, 1.0, str(i), "buy") for i, price in enumerate(prices)]
    books = [Book(t.ts_ms, t.price - 0.005, t.price + 0.005, 10.0, 10.0) for t in ticks]
    result = run_test(ticks, books, config)
    split = result["results"][0]
    if split["event_counts"]["opens"] < 1:
        raise AssertionError("synthetic test did not open overlay")
    if split["protected_ledger"] < config.protected_core_usd:
        raise AssertionError("protected ledger fell below protected core")
    return result


def main() -> None:
    if os.environ.get("SELF_TEST") == "1":
        print(json.dumps(synthetic_self_test(), indent=2))
        return
    seconds = int(os.environ.get("CAPTURE_SECONDS", "300"))
    poll = float(os.environ.get("POLL_SECONDS", "1"))
    output = Path(os.environ.get("OUTPUT_DIR", "results/live_core_overlay"))
    ticks, books, metadata = capture(seconds, poll)
    result = run_test(ticks, books, Config())
    write_outputs(ticks, books, metadata, result, output)
    print(json.dumps({
        "capture": metadata, "market": result["market"],
        "results": [{
            "name": item["name"], "final_total_equity": item["final_total_equity"],
            "return_pct": item["return_on_closure_basis_pct"],
            "protected_ledger": item["protected_ledger"],
            "overlay_cash": item["overlay_cash"],
            "overlay_liquidated": item["overlay_liquidated"],
            "event_counts": item["event_counts"],
        } for item in result["results"]],
    }, indent=2))


if __name__ == "__main__":
    main()
