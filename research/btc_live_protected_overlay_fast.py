#!/usr/bin/env python3
"""Bounded live runner for the protected-core/overlay paper strategy."""
from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import btc_live_protected_overlay as base

DEPTH_URL = "https://api.kraken.com/0/public/Depth"


def depth_book(payload: dict[str, Any], fallback: float) -> base.Book:
    result = payload.get("result")
    ts_ms = int(time.time() * 1000)
    if isinstance(result, dict):
        pair_key = next(iter(result), None)
        raw = result.get(pair_key) if pair_key else None
        if isinstance(raw, dict):
            try:
                bid = raw["bids"][0]
                ask = raw["asks"][0]
                return base.Book(ts_ms, float(bid[0]), float(ask[0]), float(bid[1]), float(ask[1]))
            except (KeyError, IndexError, TypeError, ValueError):
                pass
    half = fallback * 0.00005
    return base.Book(ts_ms, fallback - half, fallback + half, 0.0, 0.0)


def capture_fast(seconds: int, poll_seconds: float) -> tuple[list[base.Tick], list[base.Tick], list[base.Book], dict[str, Any]]:
    started = datetime.now(timezone.utc)
    start_ms = int(started.timestamp() * 1000)
    errors: list[str] = []
    mode = "posttrade"
    try:
        payload = base.http_json(base.POSTTRADE_URL, {"symbol": base.SYMBOL, "count": 1000}, retries=1)
        initial, _ = base.normalize_posttrade(payload)
        if not initial:
            raise RuntimeError("PostTrade returned no ticks")
    except Exception as exc:
        mode = "classic"
        errors.append(f"posttrade fallback: {type(exc).__name__}: {exc}")
        payload = base.http_json(base.CLASSIC_TRADES_URL, {"pair": base.PAIR}, retries=1)
        initial, _ = base.normalize_classic(payload)
    all_ticks: dict[str, base.Tick] = {tick.trade_id: tick for tick in initial}
    last_price = initial[-1].price
    books: list[base.Book] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        cycle = time.monotonic()
        try:
            if mode == "posttrade":
                payload = base.http_json(base.POSTTRADE_URL, {"symbol": base.SYMBOL, "count": 1000}, retries=1)
                ticks, _ = base.normalize_posttrade(payload)
            else:
                payload = base.http_json(base.CLASSIC_TRADES_URL, {"pair": base.PAIR}, retries=1)
                ticks, _ = base.normalize_classic(payload)
            for tick in ticks:
                all_ticks[tick.trade_id] = tick
                last_price = tick.price
        except Exception as exc:
            errors.append(f"trade poll: {type(exc).__name__}: {exc}")
        try:
            payload = base.http_json(DEPTH_URL, {"pair": base.PAIR, "count": 1}, retries=1)
            books.append(depth_book(payload, last_price))
        except Exception as exc:
            errors.append(f"book poll: {type(exc).__name__}: {exc}")
            half = last_price * 0.00005
            books.append(base.Book(int(time.time() * 1000), last_price - half, last_price + half, 0.0, 0.0))
        remaining = poll_seconds - (time.monotonic() - cycle)
        if remaining > 0:
            time.sleep(remaining)
    all_sorted = sorted(all_ticks.values(), key=lambda t: (t.ts_ms, t.trade_id))
    live = [tick for tick in all_sorted if tick.ts_ms >= start_ms]
    warmup = [tick for tick in all_sorted if tick.ts_ms < start_ms][-300:]
    metadata = {
        "source": "kraken_btc_usd_live_trades_and_classic_depth",
        "capture_started_at": started.isoformat(),
        "capture_finished_at": datetime.now(timezone.utc).isoformat(),
        "capture_seconds": seconds,
        "poll_seconds": poll_seconds,
        "warmup_trade_count": len(warmup),
        "live_trade_count": len(live),
        "book_snapshot_count": len(books),
        "errors": errors,
    }
    return warmup, live, books, metadata


def run_forward_only(warmup: list[base.Tick], live: list[base.Tick], books: list[base.Book], config: base.Config) -> dict[str, Any]:
    if len(live) < 2:
        raise RuntimeError(f"need at least two forward-only ticks, found {len(live)}")
    aligned = base.books_for_ticks(live, books)
    first_mark = (aligned[0].bid + aligned[0].ask) / 2.0
    systems = [base.ProtectedSystem.split_basis(first_mark, config), base.ProtectedSystem.strict_full_basis(first_mark, config)]
    for system in systems:
        for tick in warmup:
            system.signal.update(tick.price, config)
    for tick, book in zip(live[1:], aligned[1:]):
        for system in systems:
            system.on_tick(tick, book)
    results = [system.finish(aligned[-1]) for system in systems]
    return {
        "paper_only": True,
        "window_definition": "Only trades timestamped after capture start affect P&L; earlier trades warm indicators only.",
        "architecture": {
            "closure_basis_usd": config.closure_basis_usd,
            "split_variant": {"protected_core_usd": config.protected_core_usd, "disposable_overlay_seed_usd": config.overlay_seed_usd},
            "strict_variant": {"protected_core_usd": config.closure_basis_usd, "disposable_overlay_seed_usd": 0.0},
            "invariant": "overlay losses and liquidation cannot debit or liquidate the protected core ledger",
        },
        "market": {
            "tick_count": len(live),
            "first_time": base.iso_ms(live[0].ts_ms),
            "last_time": base.iso_ms(live[-1].ts_ms),
            "first_price": live[0].price,
            "last_price": live[-1].price,
            "high_price": max(t.price for t in live),
            "low_price": min(t.price for t in live),
            "price_change_pct": (live[-1].price / live[0].price - 1.0) * 100.0,
            "volume_btc": sum(t.quantity for t in live),
        },
        "results": results,
        "config": asdict(config),
    }


def write(output: Path, warmup: list[base.Tick], live: list[base.Tick], books: list[base.Book], metadata: dict[str, Any], result: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    base.write_outputs(live, books, metadata, result, output)
    with (output / "warmup_trades.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ts_ms", "time", "price", "quantity", "trade_id", "side"])
        writer.writeheader()
        for tick in warmup:
            writer.writerow({"ts_ms": tick.ts_ms, "time": base.iso_ms(tick.ts_ms), "price": tick.price, "quantity": tick.quantity, "trade_id": tick.trade_id, "side": tick.side})


def main() -> None:
    if os.environ.get("SELF_TEST") == "1":
        print(json.dumps(base.synthetic_self_test(), indent=2))
        return
    seconds = int(os.environ.get("CAPTURE_SECONDS", "180"))
    poll = float(os.environ.get("POLL_SECONDS", "2"))
    output = Path(os.environ.get("OUTPUT_DIR", "results/live_core_overlay_fast"))
    warmup, live, books, metadata = capture_fast(seconds, poll)
    result = run_forward_only(warmup, live, books, base.Config())
    write(output, warmup, live, books, metadata, result)
    print(json.dumps({
        "capture": metadata,
        "market": result["market"],
        "results": [{
            "name": item["name"],
            "final_total_equity": item["final_total_equity"],
            "return_pct": item["return_on_closure_basis_pct"],
            "protected_ledger": item["protected_ledger"],
            "overlay_cash": item["overlay_cash"],
            "overlay_liquidated": item["overlay_liquidated"],
            "event_counts": item["event_counts"],
        } for item in result["results"]],
    }, indent=2))


if __name__ == "__main__":
    main()
