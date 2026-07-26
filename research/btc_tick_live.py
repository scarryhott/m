#!/usr/bin/env python3
"""Capture public live BTC trade ticks and run a paper-only 50x-to-1x strategy.

Primary source: Kraken PostTrade BTC/USD. If that endpoint is unavailable, the
script falls back to Kraken's classic public Trades endpoint. No credentials or
orders are used.
"""
from __future__ import annotations

import csv
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any, Iterable

POSTTRADE_URL = "https://api.kraken.com/0/public/PostTrade"
CLASSIC_TRADES_URL = "https://api.kraken.com/0/public/Trades"
SYMBOL = "BTC/USD"
PAIR = "XBTUSD"
USER_AGENT = "btc-tick-1x-live-paper/1.0"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_ns(value: str) -> int:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, tail = text.split(".", 1)
        zone_index = max(tail.find("+"), tail.find("-"))
        if zone_index >= 0:
            fractional, zone = tail[:zone_index], tail[zone_index:]
        else:
            fractional, zone = tail, ""
        text = f"{head}.{fractional[:6].ljust(6, '0')}{zone}"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def http_json(url: str, params: dict[str, Any], retries: int = 4) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}" if query else url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise ValueError("API response was not a JSON object")
            errors = payload.get("error")
            if isinstance(errors, list) and errors:
                raise RuntimeError(f"Kraken API error: {errors}")
            return payload
        except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


@dataclass(frozen=True, slots=True)
class Tick:
    ts_ms: int
    price: float
    quantity: float
    trade_id: str
    side: str = ""


@dataclass(frozen=True, slots=True)
class Config:
    initial_margin: float = 1000.0
    initial_leverage: float = 50.0
    target_leverage: float = 1.0
    maintenance_margin_rate: float = 0.005
    taker_fee_rate: float = 0.0004
    liquidation_fee_rate: float = 0.002
    activation_equity_multiple: float = 2.0
    max_peak_equity_giveback: float = 0.25
    protected_floor_multiple: float = 1.0
    emergency_buffer_ratio: float = 0.008


@dataclass(slots=True)
class Account:
    config: Config
    wallet: float
    quantity: float
    entry_price: float
    fees: float
    liquidated: bool = False

    @classmethod
    def open(cls, price: float, config: Config) -> "Account":
        notional = config.initial_margin * config.initial_leverage
        opening_fee = notional * config.taker_fee_rate
        return cls(
            config=config,
            wallet=config.initial_margin - opening_fee,
            quantity=notional / price,
            entry_price=price,
            fees=opening_fee,
        )

    def equity(self, price: float) -> float:
        return self.wallet + self.quantity * (price - self.entry_price)

    def notional(self, price: float) -> float:
        return abs(self.quantity) * price

    def buffer(self, price: float) -> float:
        return (
            self.equity(price)
            - self.notional(price) * self.config.maintenance_margin_rate
            - self.notional(price) * self.config.taker_fee_rate
        )

    def buffer_ratio(self, price: float) -> float:
        notional = self.notional(price)
        return self.buffer(price) / notional if notional > 0 else math.inf

    def leverage(self, price: float) -> float:
        equity = self.equity(price)
        return self.notional(price) / equity if equity > 0 else math.inf

    def convert_to_one_x(self, price: float) -> float:
        target = self.config.target_leverage
        before = self.quantity
        equity_before = self.equity(price)
        fee_rate = self.config.taker_fee_rate
        denominator = price * (1.0 - target * fee_rate)
        numerator = target * (equity_before - fee_rate * before * price)
        after = max(0.0, min(before, numerator / denominator))
        close_qty = before - after
        realized = close_qty * (price - self.entry_price)
        fee = close_qty * price * fee_rate
        self.wallet += realized - fee
        self.quantity = after
        self.fees += fee
        return fee

    def liquidate(self, price: float) -> None:
        fee = self.notional(price) * self.config.liquidation_fee_rate
        remaining = max(
            0.0,
            self.equity(price)
            - self.notional(price) * self.config.maintenance_margin_rate
            - fee,
        )
        self.wallet = remaining
        self.quantity = 0.0
        self.fees += fee
        self.liquidated = True


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
            ticks.append(
                Tick(
                    ts_ms=parse_iso_ns(str(item["trade_ts"])),
                    price=float(item["price"]),
                    quantity=float(item["quantity"]),
                    trade_id=str(item["trade_id"]),
                    side=str(item.get("side", "")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return ticks, str(result.get("last_ts")) if result.get("last_ts") else None


def normalize_classic(payload: dict[str, Any]) -> tuple[list[Tick], str | None]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return [], None
    last = str(result.get("last")) if result.get("last") else None
    pair_key = next((key for key in result if key != "last"), None)
    raw = result.get(pair_key, []) if pair_key else []
    ticks: list[Tick] = []
    for index, row in enumerate(raw):
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            ts_ms = int(float(row[2]) * 1000)
            trade_id = str(row[6]) if len(row) > 6 else f"{ts_ms}:{index}:{row[0]}:{row[1]}"
            ticks.append(
                Tick(
                    ts_ms=ts_ms,
                    price=float(row[0]),
                    quantity=float(row[1]),
                    trade_id=trade_id,
                    side=str(row[3]),
                )
            )
        except (TypeError, ValueError):
            continue
    return ticks, last


def capture_live_ticks(seconds: int) -> tuple[list[Tick], dict[str, Any]]:
    started_at = utc_now_iso()
    source = "kraken_posttrade_btc_usd"
    mode = "posttrade"
    cursor: str | None = None
    all_ticks: dict[str, Tick] = {}

    try:
        payload = http_json(POSTTRADE_URL, {"symbol": SYMBOL, "count": 1000})
        initial, cursor = normalize_posttrade(payload)
        if not initial:
            raise RuntimeError("PostTrade returned no parseable BTC/USD trades")
    except Exception as exc:
        source = "kraken_classic_trades_xbtusd"
        mode = "classic"
        payload = http_json(CLASSIC_TRADES_URL, {"pair": PAIR})
        initial, cursor = normalize_classic(payload)
        if not initial:
            raise RuntimeError(f"both Kraken trade endpoints failed; PostTrade error: {exc}")

    for tick in initial:
        all_ticks[tick.trade_id] = tick

    deadline = time.monotonic() + max(0, seconds)
    polls = 0
    errors: list[str] = []
    while time.monotonic() < deadline:
        polls += 1
        try:
            if mode == "posttrade":
                params: dict[str, Any] = {"symbol": SYMBOL, "count": 1000}
                if cursor:
                    params["from_ts"] = cursor
                payload = http_json(POSTTRADE_URL, params, retries=2)
                batch, next_cursor = normalize_posttrade(payload)
            else:
                params = {"pair": PAIR}
                if cursor:
                    params["since"] = cursor
                payload = http_json(CLASSIC_TRADES_URL, params, retries=2)
                batch, next_cursor = normalize_classic(payload)
            for tick in batch:
                all_ticks[tick.trade_id] = tick
            if next_cursor:
                cursor = next_cursor
        except Exception as exc:
            errors.append(str(exc))
        time.sleep(1.0)

    ticks = sorted(all_ticks.values(), key=lambda tick: (tick.ts_ms, tick.trade_id))
    finished_at = utc_now_iso()
    metadata = {
        "source": source,
        "symbol": SYMBOL if mode == "posttrade" else PAIR,
        "capture_started_at": started_at,
        "capture_finished_at": finished_at,
        "requested_live_poll_seconds": seconds,
        "poll_count": polls,
        "poll_errors": errors[-10:],
        "tick_count": len(ticks),
    }
    return ticks, metadata


def run_strategy(ticks: Iterable[Tick], config: Config) -> dict[str, Any]:
    ticks = list(ticks)
    if not ticks:
        raise ValueError("empty tick series")
    account = Account.open(ticks[0].price, config)
    peak_equity = account.equity(ticks[0].price)
    max_drawdown = 0.0
    min_buffer = account.buffer(ticks[0].price)
    max_leverage = account.leverage(ticks[0].price)
    armed = False
    converted = False
    conversion: dict[str, Any] | None = None
    processed = 1

    if account.buffer(ticks[0].price) <= 0:
        account.liquidate(ticks[0].price)

    for tick in ticks[1:]:
        processed += 1
        if account.liquidated:
            break
        if account.buffer(tick.price) <= 0:
            account.liquidate(tick.price)
            break

        equity = account.equity(tick.price)
        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity)
        min_buffer = min(min_buffer, account.buffer(tick.price))
        max_leverage = max(max_leverage, account.leverage(tick.price))

        if converted:
            continue

        reason: str | None = None
        if account.buffer_ratio(tick.price) <= config.emergency_buffer_ratio:
            reason = "emergency_buffer"
        else:
            activation = config.initial_margin * config.activation_equity_multiple
            if not armed and peak_equity >= activation:
                armed = True
            if armed:
                trailing = peak_equity * (1.0 - config.max_peak_equity_giveback)
                absolute = config.initial_margin * config.protected_floor_multiple
                if equity <= max(trailing, absolute):
                    reason = "ratcheted_equity_floor"

        if reason:
            before_qty = account.quantity
            equity_before = account.equity(tick.price)
            fee = account.convert_to_one_x(tick.price)
            converted = True
            conversion = {
                "timestamp": datetime.fromtimestamp(tick.ts_ms / 1000, tz=timezone.utc).isoformat(),
                "ts_ms": tick.ts_ms,
                "price": tick.price,
                "reason": reason,
                "quantity_before": before_qty,
                "quantity_after": account.quantity,
                "equity_before": equity_before,
                "equity_after": account.equity(tick.price),
                "fee": fee,
            }

    final_price = ticks[min(processed, len(ticks)) - 1].price
    final_equity = account.equity(final_price)
    return {
        "status": "liquidated" if account.liquidated else "completed",
        "entry_price": ticks[0].price,
        "final_price": final_price,
        "initial_quantity_btc": config.initial_margin * config.initial_leverage / ticks[0].price,
        "final_quantity_btc": account.quantity,
        "final_equity_usd": final_equity,
        "return_pct": (final_equity / config.initial_margin - 1.0) * 100.0,
        "converted": converted,
        "conversion": conversion,
        "fees_paid_usd": account.fees,
        "max_drawdown_pct": max_drawdown * 100.0,
        "max_effective_leverage": max_leverage,
        "min_liquidation_buffer_usd": min_buffer,
        "ticks_processed": processed,
        "config": asdict(config),
    }


def grid_search(ticks: list[Tick], base: Config) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    activations = [1.0, 1.001, 1.0025, 1.005, 1.01, 1.02, 1.05, 1.10, 1.25, 1.50, 2.0]
    givebacks = [0.0, 0.001, 0.0025, 0.005, 0.01, 0.02, 0.05, 0.10, 0.20, 0.25]
    emergencies = [0.004, 0.006, 0.008, 0.010]
    completed: list[dict[str, Any]] = []
    converted: list[dict[str, Any]] = []
    for activation, giveback, emergency in product(activations, givebacks, emergencies):
        config = replace(
            base,
            activation_equity_multiple=activation,
            max_peak_equity_giveback=giveback,
            emergency_buffer_ratio=emergency,
        )
        result = run_strategy(ticks, config)
        if result["status"] == "completed":
            completed.append(result)
            if result["converted"]:
                converted.append(result)
    completed.sort(key=lambda item: item["return_pct"], reverse=True)
    converted.sort(key=lambda item: item["return_pct"], reverse=True)
    return (converted[0] if converted else None), completed[0]


def write_outputs(ticks: list[Tick], metadata: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "live_ticks.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp_utc", "ts_ms", "trade_id", "price", "quantity", "side"])
        for tick in ticks:
            writer.writerow(
                [
                    datetime.fromtimestamp(tick.ts_ms / 1000, tz=timezone.utc).isoformat(),
                    tick.ts_ms,
                    tick.trade_id,
                    f"{tick.price:.10f}",
                    f"{tick.quantity:.10f}",
                    tick.side,
                ]
            )

    base = Config()
    default_result = run_strategy(ticks, base)
    prior_winner = run_strategy(
        ticks,
        replace(base, activation_equity_multiple=1.25, max_peak_equity_giveback=0.05, emergency_buffer_ratio=0.004),
    )
    best_converted, best_overall = grid_search(ticks, base)

    first, last = ticks[0], ticks[-1]
    duration_seconds = max(0.001, (last.ts_ms - first.ts_ms) / 1000.0)
    market = {
        "first_tick_time": datetime.fromtimestamp(first.ts_ms / 1000, tz=timezone.utc).isoformat(),
        "last_tick_time": datetime.fromtimestamp(last.ts_ms / 1000, tz=timezone.utc).isoformat(),
        "tick_span_seconds": duration_seconds,
        "first_price": first.price,
        "last_price": last.price,
        "high_price": max(tick.price for tick in ticks),
        "low_price": min(tick.price for tick in ticks),
        "price_change_pct": (last.price / first.price - 1.0) * 100.0,
        "base_volume_btc": sum(tick.quantity for tick in ticks),
        "ticks_per_second": len(ticks) / duration_seconds,
    }
    payload = {
        "generated_at": utc_now_iso(),
        "paper_only": True,
        "execution_note": "Trade prices are public Kraken spot ticks; leverage, fees, maintenance margin, and conversion are simulated. No order was submitted.",
        "capture": metadata,
        "market": market,
        "default_strategy": default_result,
        "prior_reconstruction_winner": prior_winner,
        "best_converted_in_sample": best_converted,
        "best_overall_in_sample": best_overall,
        "optimization_warning": "The grid winner is in-sample on a short live window and is not evidence of future profitability.",
    }
    json_path = output_dir / "live_tick_result.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    seconds = int(os.environ.get("CAPTURE_SECONDS", "90"))
    output_dir = Path(os.environ.get("OUTPUT_DIR", "results/live_tick"))
    ticks, metadata = capture_live_ticks(seconds)
    if len(ticks) < 2:
        raise RuntimeError(f"insufficient trades captured: {len(ticks)}")
    payload = write_outputs(ticks, metadata, output_dir)
    summary = {
        "capture": payload["capture"],
        "market": payload["market"],
        "default_strategy": payload["default_strategy"],
        "prior_reconstruction_winner": payload["prior_reconstruction_winner"],
        "best_converted_in_sample": payload["best_converted_in_sample"],
        "best_overall_in_sample": payload["best_overall_in_sample"],
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
