#!/usr/bin/env python3
"""Backtest protected spot core + isolated surplus-funded BTC futures overlay.

The first 50x futures regime is allowed once. On its first admissible closure:
- up to the original $1,000 is converted into a protected spot BTC core;
- only cash above that protected amount funds an isolated futures overlay;
- overlay profits are harvested into additional spot BTC;
- overlay losses and liquidation cannot consume the spot core.
"""
from __future__ import annotations

import csv
import itertools
import json
import math
import statistics
import sys
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
for candidate in [HERE, HERE / "research", Path("research")]:
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import btc_recursive_backtest_corrected as corrected

bt = corrected.module
U = timezone.utc


@dataclass(frozen=True)
class PCfg:
    initial: bt.C = bt.C(
        act=0.02,
        rev=0.001,
        breakout=0.001,
        maxlev=5.0,
        budget=0.10,
    )
    spot_fee: float = 0.001
    spot_slip_bps: float = 1.0
    overlay_fee: float = 0.0004
    overlay_slip_bps: float = 1.0
    overlay_mmr: float = 0.005
    overlay_liq_fee: float = 0.002
    funding_mult: float = 1.0
    activation: float = 0.02
    reversal: float = 0.001
    giveback: float = 0.20
    trend_break: int = 3
    emergency_buffer: float = 0.02
    stop_loss: float = 0.10
    fast: int = 12
    slow: int = 48
    confirm: int = 4
    cooldown: int = 12
    breakout: float = 0.0025
    breakout_vol_mult: float = 1.5
    vol_window: int = 96
    vol_horizon: int = 32
    vol_mult: float = 3.0
    min_adverse: float = 0.005
    loss_budget: float = 0.10
    liquidation_reserve: float = 0.25
    min_leverage: float = 1.25
    max_leverage: float = 3.0
    harvest_fraction: float = 1.0
    min_overlay_cash: float = 10.0
    close_liq: bool = False


class FastSig:
    def __init__(self, price: float, cfg: PCfg):
        self.fast = self.slow = self.prev_fast = self.prev_price = price
        self.returns: deque[float] = deque()
        self.sum = 0.0
        self.sum_sq = 0.0
        self.af = 2.0 / (cfg.fast + 1)
        self.aslow = 2.0 / (cfg.slow + 1)
        self.window = cfg.vol_window

    def update(self, price: float) -> bool:
        self.prev_fast = self.fast
        value = math.log(price / self.prev_price)
        self.prev_price = price
        if len(self.returns) >= self.window:
            old = self.returns.popleft()
            self.sum -= old
            self.sum_sq -= old * old
        self.returns.append(value)
        self.sum += value
        self.sum_sq += value * value
        self.fast = self.af * price + (1.0 - self.af) * self.fast
        self.slow = self.aslow * price + (1.0 - self.aslow) * self.slow
        return self.fast > self.slow and self.fast >= self.prev_fast

    def sigma(self) -> float:
        n = len(self.returns)
        if n < 3:
            return 0.0
        return math.sqrt(max(0.0, self.sum_sq / n - (self.sum / n) ** 2))


@dataclass
class Overlay:
    cash: float
    quantity: float = 0.0
    entry: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    turnover: float = 0.0
    liquidated: bool = False

    @property
    def flat(self) -> bool:
        return self.quantity == 0.0

    def equity(self, price: float) -> float:
        if self.flat:
            return self.cash
        return self.cash + self.quantity * (price - self.entry)

    def notional(self, price: float) -> float:
        return abs(self.quantity) * price

    def leverage(self, price: float) -> float:
        equity = self.equity(price)
        return self.notional(price) / equity if equity > 0 else math.inf

    def buffer(self, price: float, cfg: PCfg) -> float:
        return self.equity(price) - self.notional(price) * (
            cfg.overlay_mmr + cfg.overlay_liq_fee
        )

    def open_long(self, target_leverage: float, price: float, cfg: PCfg) -> bool:
        if not self.flat or self.cash <= 0:
            return False
        slippage = cfg.overlay_slip_bps / 1e4
        notional = target_leverage * self.cash / (
            1.0 + target_leverage * cfg.overlay_fee
        )
        execution = price * (1.0 + slippage)
        fee = notional * cfg.overlay_fee
        quantity = notional / execution
        if quantity <= 0 or self.cash - fee <= 0:
            return False
        self.cash -= fee
        self.quantity = quantity
        self.entry = execution
        self.fees += fee
        self.turnover += notional
        return True

    def close_long(self, price: float, cfg: PCfg) -> float:
        if self.flat:
            return self.cash
        slippage = cfg.overlay_slip_bps / 1e4
        execution = price * (1.0 - slippage)
        notional = self.quantity * execution
        fee = notional * cfg.overlay_fee
        self.cash += self.quantity * (execution - self.entry) - fee
        self.fees += fee
        self.turnover += notional
        self.quantity = 0.0
        self.entry = execution
        if self.cash < 0:
            self.cash = 0.0
        return self.cash

    def apply_funding(self, mark_price: float, rate: float, cfg: PCfg) -> None:
        if self.flat or rate == 0:
            return
        payment = self.quantity * mark_price * rate * cfg.funding_mult
        self.cash -= payment
        self.funding += payment

    def liquidate(self, mark_price: float, cfg: PCfg) -> None:
        if self.flat:
            return
        equity = self.equity(mark_price)
        charge = self.notional(mark_price) * (
            cfg.overlay_mmr + cfg.overlay_liq_fee
        )
        self.cash = max(0.0, equity - charge)
        self.quantity = 0.0
        self.entry = mark_price
        self.liquidated = True


def close_futures_to_cash(account: bt.A, price: float, cfg: bt.C) -> float:
    slippage = cfg.slip / 1e4
    execution = price * (1.0 - slippage)
    notional = account.q * execution
    fee = notional * cfg.fee
    return max(0.0, account.w + account.q * (execution - account.e) - fee)


def buy_spot(cash: float, price: float, cfg: PCfg) -> tuple[float, float]:
    if cash <= 0:
        return 0.0, 0.0
    execution = price * (1.0 + cfg.spot_slip_bps / 1e4)
    fee = cash * cfg.spot_fee
    quantity = max(0.0, cash - fee) / execution
    return quantity, fee


def initial_closure(m: bt.M, cfg: PCfg) -> dict:
    c = cfg.initial
    price = float(m.o[0])
    notional = c.origin * c.lev
    execution = price * (1.0 + c.slip / 1e4)
    account = bt.A(
        c.origin - notional * c.fee,
        notional / execution,
        execution,
        notional * c.fee,
        0.0,
        notional,
    )
    signal = bt.Sig(price, c)
    peak_equity = account.eq(price)
    peak_price = price
    armed = False
    down_count = 0
    pending = False
    max_drawdown = 0.0
    global_peak = peak_equity

    for i in range(len(m.t)):
        timestamp = int(m.t[i])
        open_price = float(m.o[i])

        if pending:
            cash = close_futures_to_cash(account, open_price, c)
            if cash >= c.origin:
                return {
                    "status": "closed",
                    "index": i,
                    "time": bt.iso(timestamp),
                    "price": open_price,
                    "cash": cash,
                    "fees": account.fees
                    + account.q * open_price * (1.0 - c.slip / 1e4) * c.fee,
                    "funding": account.fund,
                    "max_drawdown_pct": min(100.0, max_drawdown * 100.0),
                }
            pending = False

        if m.f[i]:
            payment = account.q * float(m.mo[i]) * float(m.f[i]) * c.fund_mult
            account.w -= payment
            account.fund += payment

        liquidation_price = float(m.mc[i] if c.close_liq else m.ml[i])
        if account.buf(liquidation_price, c) <= 0:
            return {
                "status": "liquidated",
                "index": i,
                "time": bt.iso(timestamp),
                "price": liquidation_price,
                "cash": 0.0,
                "fees": account.fees,
                "funding": account.fund,
                "max_drawdown_pct": 100.0,
            }

        close_price = float(m.c[i])
        up = signal.up(close_price)
        equity = account.eq(close_price)
        global_peak = max(global_peak, equity)
        max_drawdown = max(
            max_drawdown,
            (global_peak - equity) / global_peak if global_peak else 0.0,
        )
        peak_equity = max(peak_equity, equity)
        peak_price = max(peak_price, close_price)
        down_count = down_count + 1 if not up and signal.f < signal.s else 0
        armed = armed or peak_equity >= c.origin * (1.0 + c.act)
        notional_now = account.n(float(m.mc[i]))
        buffer_ratio = (
            account.buf(float(m.mc[i]), c) / notional_now if notional_now else 99
        )
        reason = None
        if buffer_ratio <= c.emerg:
            reason = "emergency"
        elif armed:
            projected_cash = close_futures_to_cash(account, close_price, c)
            preserved = projected_cash >= c.origin
            trail = max(c.origin, peak_equity * (1.0 - c.give))
            if preserved and close_price <= peak_price * (1.0 - c.rev):
                reason = "reversal"
            elif preserved and equity <= trail:
                reason = "giveback"
            elif preserved and down_count >= c.down:
                reason = "trend"
        if reason:
            pending = True

    return {
        "status": "unclosed",
        "index": len(m.t) - 1,
        "time": bt.iso(int(m.t[-1])),
        "price": float(m.c[-1]),
        "cash": account.eq(float(m.c[-1])),
        "fees": account.fees,
        "funding": account.fund,
        "max_drawdown_pct": min(100.0, max_drawdown * 100.0),
    }


def target_overlay_leverage(signal: FastSig, cfg: PCfg) -> tuple[float, float]:
    adverse = max(
        cfg.min_adverse,
        cfg.vol_mult * signal.sigma() * math.sqrt(cfg.vol_horizon),
    )
    risk_cap = cfg.loss_budget / adverse
    liquidation_cap = (1.0 - cfg.liquidation_reserve) / (
        adverse + cfg.overlay_mmr + cfg.overlay_liq_fee
    )
    return min(cfg.max_leverage, risk_cap, liquidation_cap), adverse


def run_overlay(
    m: bt.M,
    cfg: PCfg,
    core_quantity: float,
    overlay_cash: float,
    start_index: int = 0,
    events: bool = False,
) -> dict:
    if start_index >= len(m.t):
        raise RuntimeError("overlay start index beyond market data")
    first_price = float(m.o[start_index])
    signal = FastSig(first_price, cfg)
    overlay = Overlay(max(0.0, overlay_cash))
    overlay_basis = overlay.cash
    basis_price = first_price
    phase = "flat"
    pending: tuple[str, str, float] | None = None
    peak_overlay_equity = overlay.cash
    peak_price = first_price
    armed = False
    up_count = 0
    down_count = 0
    cooldown = cfg.cooldown
    portfolio_peak = core_quantity * first_price + overlay.cash
    max_drawdown = 0.0
    minimum_portfolio = portfolio_peak
    minimum_overlay_buffer = math.inf
    maximum_overlay_leverage = 0.0
    entries = exits = harvests = overlay_liquidations = 0
    harvested_cash = 0.0
    harvested_btc = 0.0
    event_log: list[dict] = []

    for i in range(start_index, len(m.t)):
        timestamp = int(m.t[i])
        open_price = float(m.o[i])

        if pending is not None:
            action, reason, target = pending
            pending = None
            if action == "open" and overlay.flat and overlay.cash >= cfg.min_overlay_cash:
                if overlay.open_long(target, open_price, cfg):
                    phase = "long"
                    entries += 1
                    peak_overlay_equity = overlay.equity(open_price)
                    peak_price = open_price
                    armed = False
                    up_count = down_count = 0
                    if events:
                        event_log.append(
                            {
                                "type": "overlay_open",
                                "time": bt.iso(timestamp),
                                "price": open_price,
                                "target_leverage": target,
                                "cash_after_fee": overlay.cash,
                                "core_btc": core_quantity,
                            }
                        )
            elif action == "close" and not overlay.flat:
                cash_before = overlay.cash
                equity_before = overlay.equity(open_price)
                overlay.close_long(open_price, cfg)
                exits += 1
                profit = max(0.0, overlay.cash - overlay_basis)
                harvest_cash = profit * cfg.harvest_fraction
                quantity = 0.0
                if harvest_cash > 0:
                    quantity, spot_fee = buy_spot(harvest_cash, open_price, cfg)
                    core_quantity += quantity
                    overlay.cash -= harvest_cash
                    overlay.fees += spot_fee
                    harvested_cash += harvest_cash
                    harvested_btc += quantity
                    harvests += 1
                overlay_basis = overlay.cash
                basis_price = open_price
                phase = "flat"
                cooldown = cfg.cooldown
                armed = False
                up_count = down_count = 0
                peak_overlay_equity = overlay.cash
                peak_price = open_price
                if events:
                    event_log.append(
                        {
                            "type": "overlay_close",
                            "time": bt.iso(timestamp),
                            "price": open_price,
                            "reason": reason,
                            "equity_before": equity_before,
                            "cash_before": cash_before,
                            "cash_after": overlay.cash,
                            "harvest_cash": harvest_cash,
                            "harvested_btc": quantity,
                            "core_btc": core_quantity,
                        }
                    )

        if m.f[i] and not overlay.flat:
            overlay.apply_funding(float(m.mo[i]), float(m.f[i]), cfg)

        liquidation_price = float(m.mc[i] if cfg.close_liq else m.ml[i])
        if not overlay.flat and overlay.buffer(liquidation_price, cfg) <= 0:
            overlay.liquidate(liquidation_price, cfg)
            phase = "dead"
            overlay_liquidations += 1
            if events:
                event_log.append(
                    {
                        "type": "overlay_liquidation",
                        "time": bt.iso(timestamp),
                        "price": liquidation_price,
                        "core_btc": core_quantity,
                    }
                )

        close_price = float(m.c[i])
        up = signal.update(close_price)
        overlay_equity = overlay.equity(close_price)
        core_value = core_quantity * close_price
        portfolio = core_value + overlay_equity
        portfolio_peak = max(portfolio_peak, portfolio)
        minimum_portfolio = min(minimum_portfolio, portfolio)
        max_drawdown = max(
            max_drawdown,
            (portfolio_peak - portfolio) / portfolio_peak if portfolio_peak else 0.0,
        )
        if not overlay.flat:
            minimum_overlay_buffer = min(
                minimum_overlay_buffer,
                overlay.buffer(float(m.mc[i]), cfg),
            )
            maximum_overlay_leverage = max(
                maximum_overlay_leverage,
                overlay.leverage(close_price),
            )

        if phase == "long":
            peak_overlay_equity = max(peak_overlay_equity, overlay_equity)
            peak_price = max(peak_price, close_price)
            down_count = down_count + 1 if not up and signal.fast < signal.slow else 0
            armed = armed or peak_overlay_equity >= overlay_basis * (
                1.0 + cfg.activation
            )
            reason = None
            notional = overlay.notional(float(m.mc[i]))
            buffer_ratio = (
                overlay.buffer(float(m.mc[i]), cfg) / notional if notional else 99
            )
            if overlay_equity <= overlay_basis * (1.0 - cfg.stop_loss):
                reason = "stop_loss"
            elif buffer_ratio <= cfg.emergency_buffer:
                reason = "emergency_buffer"
            elif armed:
                trail = max(
                    overlay_basis,
                    peak_overlay_equity * (1.0 - cfg.giveback),
                )
                if close_price <= peak_price * (1.0 - cfg.reversal):
                    reason = "reversal"
                elif overlay_equity <= trail:
                    reason = "giveback"
                elif down_count >= cfg.trend_break:
                    reason = "trend"
            if reason:
                pending = ("close", reason, 0.0)

        elif phase == "flat" and not overlay.liquidated:
            cooldown = max(0, cooldown - 1)
            up_count = up_count + 1 if up else 0
            breakout_move = max(
                cfg.breakout,
                cfg.breakout_vol_mult
                * signal.sigma()
                * math.sqrt(cfg.vol_horizon),
            )
            if (
                overlay.cash >= cfg.min_overlay_cash
                and cooldown == 0
                and up_count >= cfg.confirm
                and close_price >= basis_price * (1.0 + breakout_move)
            ):
                target, _ = target_overlay_leverage(signal, cfg)
                if target >= cfg.min_leverage:
                    pending = ("open", "breakout", target)

    final_price = float(m.c[-1])
    final_overlay_equity = overlay.equity(final_price)
    final_core_value = core_quantity * final_price
    final_portfolio = final_core_value + final_overlay_equity
    start_value = core_quantity * first_price + overlay_cash
    years = max(
        (int(m.t[-1]) - int(m.t[start_index])) / (365.2425 * 86400000),
        1 / 365.2425,
    )
    return {
        "status": "completed_core_survived",
        "start": bt.iso(int(m.t[start_index])),
        "end": bt.iso(int(m.t[-1])),
        "start_price": first_price,
        "final_price": final_price,
        "start_portfolio": start_value,
        "final_portfolio": final_portfolio,
        "return_pct": (final_portfolio / start_value - 1.0) * 100.0
        if start_value > 0
        else -100.0,
        "cagr_pct": (final_portfolio / start_value) ** (1 / years) * 100.0 - 100.0
        if start_value > 0 and final_portfolio > 0
        else -100.0,
        "max_drawdown_pct": min(100.0, max_drawdown * 100.0),
        "minimum_portfolio": minimum_portfolio,
        "core_btc": core_quantity,
        "core_value": final_core_value,
        "overlay_cash": overlay.cash,
        "overlay_equity": final_overlay_equity,
        "overlay_liquidated": overlay.liquidated,
        "overlay_liquidations": overlay_liquidations,
        "overlay_entries": entries,
        "overlay_exits": exits,
        "harvests": harvests,
        "harvested_cash": harvested_cash,
        "harvested_btc": harvested_btc,
        "fees": overlay.fees,
        "funding": overlay.funding,
        "turnover": overlay.turnover,
        "maximum_overlay_leverage": maximum_overlay_leverage,
        "minimum_overlay_buffer": minimum_overlay_buffer
        if math.isfinite(minimum_overlay_buffer)
        else None,
        "events": event_log if events else None,
        "config": config_dict(cfg),
    }


def config_dict(cfg: PCfg) -> dict:
    output = asdict(cfg)
    output["initial"] = asdict(cfg.initial)
    return output


def run_full(m: bt.M, cfg: PCfg, events: bool = False) -> dict:
    first = initial_closure(m, cfg)
    if first["status"] != "closed":
        return {
            "status": "initial_50x_liquidated",
            "initial": first,
            "final_portfolio": 0.0,
            "return_pct": -100.0,
            "max_drawdown_pct": 100.0,
            "core_btc": 0.0,
            "overlay_cash": 0.0,
            "events": [],
            "config": config_dict(cfg),
        }
    protected_cash = min(cfg.initial.origin, first["cash"])
    core_quantity, core_fee = buy_spot(protected_cash, first["price"], cfg)
    overlay_cash = max(0.0, first["cash"] - protected_cash)
    overlay = run_overlay(
        m,
        cfg,
        core_quantity,
        overlay_cash,
        start_index=first["index"],
        events=events,
    )
    total_fees = first["fees"] + core_fee + overlay["fees"]
    total_funding = first["funding"] + overlay["funding"]
    return {
        "status": overlay["status"],
        "initial": first,
        "protected_cash_at_closure": protected_cash,
        "initial_core_btc": core_quantity,
        "initial_overlay_cash": overlay_cash,
        **overlay,
        "fees": total_fees,
        "funding": total_funding,
        "return_on_original_pct": (overlay["final_portfolio"] / cfg.initial.origin - 1)
        * 100.0,
    }


def core_only(m: bt.M, cfg: PCfg, all_cash_to_core: bool = False) -> dict:
    first = initial_closure(m, cfg)
    if first["status"] != "closed":
        return {
            "status": "initial_50x_liquidated",
            "final_portfolio": 0.0,
            "return_pct": -100.0,
            "initial": first,
        }
    core_cash = first["cash"] if all_cash_to_core else min(cfg.initial.origin, first["cash"])
    reserve = 0.0 if all_cash_to_core else max(0.0, first["cash"] - core_cash)
    quantity, fee = buy_spot(core_cash, first["price"], cfg)
    prices = m.c[first["index"] :]
    values = quantity * prices + reserve
    peak = np.maximum.accumulate(values)
    return {
        "status": "completed",
        "final_portfolio": float(values[-1]),
        "return_pct": float((values[-1] / cfg.initial.origin - 1) * 100.0),
        "max_drawdown_pct": float(np.max((peak - values) / peak) * 100.0),
        "core_btc": quantity,
        "reserve_cash": reserve,
        "fees": first["fees"] + fee,
        "funding": first["funding"],
        "initial": first,
    }


def scale_cfg(cfg: PCfg, factor: int) -> PCfg:
    return replace(
        cfg,
        fast=max(2, round(cfg.fast / factor)),
        slow=max(4, round(cfg.slow / factor)),
        trend_break=max(1, round(cfg.trend_break / factor)),
        confirm=max(1, round(cfg.confirm / factor)),
        cooldown=max(1, round(cfg.cooldown / factor)),
        vol_window=max(8, round(cfg.vol_window / factor)),
        vol_horizon=max(1, round(cfg.vol_horizon / factor)),
    )


def seeded_overlay_block(m: bt.M, cfg: PCfg, surplus_ratio: float) -> dict:
    first_price = float(m.o[0])
    core_cash = cfg.initial.origin
    core_quantity, _ = buy_spot(core_cash, first_price, cfg)
    overlay_cash = cfg.initial.origin * surplus_ratio
    return run_overlay(m, cfg, core_quantity, overlay_cash, 0, False)


def optimize_overlay(m: bt.M, base: PCfg, surplus_ratio: float) -> tuple[list[dict], PCfg]:
    grid: list[PCfg] = []
    for activation, reversal, breakout, max_leverage, harvest in itertools.product(
        [0.01, 0.02, 0.04],
        [0.001, 0.002],
        [0.001, 0.0025],
        [2.0, 3.0, 5.0],
        [0.5, 1.0],
    ):
        grid.append(
            replace(
                base,
                activation=activation,
                reversal=reversal,
                breakout=breakout,
                max_leverage=max_leverage,
                harvest_fraction=harvest,
            )
        )
    cuts = np.linspace(0, len(m.t), 5, dtype=int)
    output: list[dict] = []
    for cfg in grid:
        block_results = []
        for start, end in zip(cuts[:-1], cuts[1:]):
            block = bt.M(
                *[
                    array[start:end]
                    for array in [
                        m.t,
                        m.o,
                        m.h,
                        m.l,
                        m.c,
                        m.mo,
                        m.mh,
                        m.ml,
                        m.mc,
                        m.f,
                    ]
                ],
                m.meta,
            )
            block_results.append(
                seeded_overlay_block(block, scale_cfg(cfg, 5), surplus_ratio)
            )
        returns = [item["return_pct"] for item in block_results]
        drawdowns = [item["max_drawdown_pct"] for item in block_results]
        deaths = sum(item["overlay_liquidated"] for item in block_results)
        score = (
            statistics.median(returns)
            + 0.25 * min(returns)
            - 0.20 * max(drawdowns)
            - 25.0 * deaths
        )
        output.append(
            {
                "score": score,
                "returns": returns,
                "worst_drawdown": max(drawdowns),
                "overlay_deaths": deaths,
                "config": config_dict(cfg),
            }
        )
    output.sort(key=lambda item: item["score"], reverse=True)
    winner = output[0]["config"].copy()
    winner["initial"] = bt.C(**winner["initial"])
    return output[:10], PCfg(**winner)


def main() -> None:
    output = Path("results/protected_core_backtest")
    output.mkdir(parents=True, exist_ok=True)
    market = bt.market(date(2021, 7, 1), date(2026, 7, 26), Path("data/binance"))
    entry = bt.ms("2021-07-26T15:00:00Z")
    split = bt.ms("2024-01-01T00:00:00Z")
    full = market.sl(entry, market.t[-1])
    if len(full.t) == 0 or full.t[0] > entry + 60000:
        raise RuntimeError(
            f"requested entry unavailable: requested={bt.iso(entry)} "
            f"first={bt.iso(full.t[0]) if len(full.t) else None}"
        )
    train = market.sl(entry, split - 1)
    test = market.sl(split, market.t[-1])
    base = PCfg()

    first = initial_closure(full, base)
    if first["status"] != "closed":
        raise RuntimeError("base initial closure did not survive")
    surplus_ratio = max(0.0, first["cash"] - base.initial.origin) / base.initial.origin

    top, winner = optimize_overlay(train.rs(5), base, surplus_ratio)
    top_oos = []
    for candidate in top:
        cfg_data = candidate["config"].copy()
        cfg_data["initial"] = bt.C(**cfg_data["initial"])
        cfg = PCfg(**cfg_data)
        top_oos.append(
            {
                "train": candidate,
                "oos": seeded_overlay_block(test, cfg, surplus_ratio),
            }
        )

    protected = run_full(full, winner, True)
    protected_core_only = core_only(full, winner, False)
    all_core = core_only(full, winner, True)
    prior_one_way = bt.run(full, replace(winner.initial, reentry=False), "50", True)
    static_50 = bt.run(
        full,
        replace(winner.initial, act=99, reentry=False, emerg=-99),
        "50",
    )
    spot_from_start = bt.spot(full)

    entry_timing = []
    for hour in range(24):
        requested = bt.ms(f"2021-07-26T{hour:02d}:00:00Z")
        slice_ = market.sl(requested, market.t[-1])
        if len(slice_.t) == 0 or slice_.t[0] > requested + 60000:
            entry_timing.append(
                {
                    "hour": hour,
                    "status": "missing_entry",
                    "final_portfolio": 0.0,
                    "return_pct": -100.0,
                    "initial_overlay_cash": 0.0,
                    "overlay_liquidated": False,
                }
            )
            continue
        result = run_full(slice_, winner, False)
        entry_timing.append(
            {
                "hour": hour,
                "entry": float(slice_.o[0]),
                "status": result["status"],
                "final_portfolio": result["final_portfolio"],
                "return_pct": result.get("return_on_original_pct", -100.0),
                "initial_overlay_cash": result.get("initial_overlay_cash", 0.0),
                "overlay_liquidated": result.get("overlay_liquidated", False),
            }
        )

    stress = []
    scenarios = [
        ("base", winner),
        ("overlay_slip0", replace(winner, overlay_slip_bps=0.0)),
        ("overlay_slip5", replace(winner, overlay_slip_bps=5.0)),
        ("overlay_fee6", replace(winner, overlay_fee=0.0006)),
        ("overlay_fee10", replace(winner, overlay_fee=0.001)),
        ("overlay_mmr1", replace(winner, overlay_mmr=0.01)),
        ("fund0", replace(winner, funding_mult=0.0)),
        ("fund2", replace(winner, funding_mult=2.0)),
        ("cap2", replace(winner, max_leverage=2.0)),
        ("cap5", replace(winner, max_leverage=5.0)),
        ("harvest100", replace(winner, harvest_fraction=1.0)),
        ("stop5", replace(winner, stop_loss=0.05)),
        ("close_liq", replace(winner, close_liq=True)),
    ]
    for name, cfg in scenarios:
        result = run_full(full, cfg, False)
        stress.append(
            {
                "scenario": name,
                "status": result["status"],
                "final_portfolio": result["final_portfolio"],
                "return_pct": result.get("return_on_original_pct", -100.0),
                "max_drawdown_pct": result.get("max_drawdown_pct", 100.0),
                "core_btc": result.get("core_btc", 0.0),
                "overlay_liquidated": result.get("overlay_liquidated", False),
                "overlay_entries": result.get("overlay_entries", 0),
                "harvested_cash": result.get("harvested_cash", 0.0),
                "fees": result.get("fees", 0.0),
                "funding": result.get("funding", 0.0),
            }
        )

    report = {
        "generated": datetime.now(U).isoformat(),
        "data": market.meta,
        "periods": {
            "entry": bt.iso(int(full.t[0])),
            "end": bt.iso(int(full.t[-1])),
            "split": bt.iso(split),
        },
        "architecture": {
            "core": "spot BTC; never used as futures collateral; quantity only increases",
            "overlay": "isolated futures funded only by first-closure surplus and retained overlay cash",
            "harvest": "configured fraction of profitable overlay closure buys more spot BTC",
            "failure_boundary": "overlay may liquidate to zero while spot core remains",
        },
        "method": {
            "training": "72 configs, four seeded blocks, 5-minute bars with scaled time constants",
            "test": "frozen training winner on untouched 2024+ one-minute data",
            "execution": "signals at close, action next open, archived funding, mark-low liquidation",
        },
        "initial_closure": first,
        "surplus_ratio": surplus_ratio,
        "winner": config_dict(winner),
        "top_train_oos": top_oos,
        "results": {
            "protected_core_overlay": protected,
            "protected_core_plus_cash": protected_core_only,
            "all_closure_cash_to_spot": all_core,
            "prior_one_way_perpetual": prior_one_way,
            "static_50x": static_50,
            "spot_from_start": spot_from_start,
        },
        "entry_timing": entry_timing,
        "initial_survival_pct": 100.0
        * sum(item["status"] != "initial_50x_liquidated" for item in entry_timing)
        / len(entry_timing),
        "portfolio_survival_pct": 100.0
        * sum(item["final_portfolio"] > 0 for item in entry_timing)
        / len(entry_timing),
        "stress": stress,
        "limits": [
            "one-minute OHLC has unknown intraminute ordering",
            "fixed maintenance margin rather than historical tier reconstruction",
            "spot and futures execution use fixed slippage rather than order-book depth",
            "long-only overlay",
            "paper backtest is not evidence of future profit",
        ],
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for filename, rows in [
        ("entry_timing.csv", entry_timing),
        ("stress.csv", stress),
    ]:
        with (output / filename).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    summary = {
        "data": market.meta,
        "winner": config_dict(winner),
        "protected_core_overlay": {
            key: protected[key]
            for key in [
                "status",
                "final_portfolio",
                "return_on_original_pct",
                "max_drawdown_pct",
                "core_btc",
                "overlay_liquidated",
                "overlay_entries",
                "overlay_exits",
                "harvests",
                "harvested_cash",
                "fees",
                "funding",
            ]
        },
        "protected_core_plus_cash": protected_core_only,
        "all_closure_cash_to_spot": all_core,
        "prior_one_way_perpetual": {
            key: prior_one_way[key]
            for key in ["status", "final_equity", "return_pct", "max_drawdown_pct"]
        },
        "spot_from_start": spot_from_start,
        "initial_survival_pct": report["initial_survival_pct"],
        "portfolio_survival_pct": report["portfolio_survival_pct"],
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
