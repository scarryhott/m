#!/usr/bin/env python3
"""Paper-only recursive BTC tick strategy: leverage -> 1x basis -> new leverage."""
from __future__ import annotations

import csv, json, math, os, statistics
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class Tick:
    ms: int
    p: float
    q: float
    trade_id: str


@dataclass(frozen=True)
class Cfg:
    origin: float = 1000.0
    initial_lev: float = 50.0
    mmr: float = 0.005
    fee: float = 0.0004
    liq_fee: float = 0.002
    activation_return: float = 0.02
    price_reversal: float = 0.00002
    equity_giveback: float = 0.20
    trend_break_ticks: int = 3
    emergency_buffer: float = 0.008
    fast_ticks: int = 12
    slow_ticks: int = 48
    trend_confirm_ticks: int = 4
    cooldown_ticks: int = 12
    min_breakout: float = 0.0005
    breakout_vol_mult: float = 1.5
    vol_window: int = 96
    vol_horizon: int = 32
    vol_mult: float = 3.0
    min_adverse_move: float = 0.005
    loss_budget: float = 0.10
    liquidation_reserve: float = 0.20
    min_reentry_lev: float = 1.25
    max_reentry_lev: float = 20.0


@dataclass
class Acct:
    c: Cfg
    wallet: float
    qty: float
    entry: float
    fees: float = 0.0
    liquidated: bool = False

    @classmethod
    def initial(cls, p, c):
        n = c.origin * c.initial_lev
        f = n * c.fee
        return cls(c, c.origin - f, n / p, p, f)

    @classmethod
    def basis(cls, equity, p, c):
        return cls(c, equity, equity / p, p)

    def equity(self, p):
        return self.wallet + self.qty * (p - self.entry)

    def notional(self, p):
        return abs(self.qty) * p

    def lev(self, p):
        e = self.equity(p)
        return self.notional(p) / e if e > 0 else math.inf

    def buffer(self, p):
        n = self.notional(p)
        return self.equity(p) - n * (self.c.mmr + self.c.fee)

    def buffer_ratio(self, p):
        n = self.notional(p)
        return self.buffer(p) / n if n else math.inf

    def project(self, target, p):
        e = self.equity(p)
        n = self.notional(p)
        cur = n / e if e > 0 else math.inf
        if target >= cur:
            add_n = max(0.0, (target * e - n) / (1 + target * self.c.fee))
            return self.qty + add_n / p, add_n * self.c.fee, e - add_n * self.c.fee
        keep_n = max(0.0, target * (e - self.c.fee * n) / (1 - target * self.c.fee))
        q2 = min(self.qty, keep_n / p)
        close_n = (self.qty - q2) * p
        return q2, close_n * self.c.fee, e - close_n * self.c.fee

    def adjust(self, target, p):
        q2, f, e2 = self.project(target, p)
        dq = q2 - self.qty
        if dq > 0:
            self.entry = (self.qty * self.entry + dq * p) / q2
            self.qty = q2
            self.wallet -= f
        elif dq < 0:
            close = -dq
            self.wallet += close * (p - self.entry) - f
            self.qty = q2
        self.fees += f
        return {"fee": f, "equity_after": e2, "quantity_after": q2}

    def normalize(self, p):
        e = self.equity(p)
        self.wallet, self.entry = e, p
        return e

    def liquidate(self, p):
        n = self.notional(p)
        f = n * self.c.liq_fee
        self.wallet = max(0.0, self.equity(p) - n * self.c.mmr - f)
        self.qty, self.entry, self.fees, self.liquidated = 0.0, p, self.fees + f, True


class Signal:
    def __init__(self, p, c):
        self.fast = self.slow = self.prev_fast = self.prev_p = p
        self.r = deque(maxlen=c.vol_window)

    def update(self, p, c):
        self.prev_fast = self.fast
        if p > 0 and self.prev_p > 0:
            self.r.append(math.log(p / self.prev_p))
        self.prev_p = p
        af, slow = 2 / (c.fast_ticks + 1), 2 / (c.slow_ticks + 1)
        self.fast = af * p + (1 - af) * self.fast
        self.slow = slow * p + (1 - slow) * self.slow

    def sigma(self):
        return statistics.pstdev(self.r) if len(self.r) >= 3 else 0.0

    def up(self):
        return self.fast > self.slow and self.fast >= self.prev_fast

    def down(self):
        return self.fast < self.slow and self.fast <= self.prev_fast


def fee_cap(a, p, floor, upper):
    cur = a.lev(p)
    if a.project(upper, p)[2] >= floor:
        return upper
    lo, hi = cur, upper
    for _ in range(60):
        mid = (lo + hi) / 2
        if a.project(mid, p)[2] >= floor:
            lo = mid
        else:
            hi = mid
    return lo


def new_leverage(a, p, basis_eq, s, c):
    sigma = s.sigma()
    adverse = max(c.min_adverse_move, c.vol_mult * sigma * math.sqrt(c.vol_horizon))
    risk = c.loss_budget / adverse
    liquid = (1 - c.liquidation_reserve) / (adverse + c.mmr + c.fee)
    fees = fee_cap(a, p, basis_eq, c.max_reentry_lev)
    target = max(1.0, min(c.max_reentry_lev, risk, liquid, fees))
    return {"target": target, "sigma": sigma, "adverse_pct": adverse * 100,
            "risk_cap": risk, "liquidation_cap": liquid, "fee_cap": fees}


def load_ticks(path, after_ms=None):
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            ms = int(row["ts_ms"])
            if after_ms is not None and ms < after_ms:
                continue
            out.append(Tick(ms, float(row["price"]), float(row.get("quantity") or 0), row.get("trade_id") or str(i)))
    out.sort(key=lambda t: (t.ms, t.trade_id))
    if len(out) < 2:
        raise RuntimeError(f"need at least two ticks, found {len(out)}")
    return out


def run(ticks, c, seed_eq=None, seed_p=None, seed_ms=None):
    seeded = seed_eq is not None
    if seeded:
        a, basis_eq, basis_p, basis_ms, phase, rid = Acct.basis(seed_eq, seed_p, c), seed_eq, seed_p, seed_ms or ticks[0].ms, "basis", 1
    else:
        a, basis_eq, basis_p, basis_ms, phase, rid = Acct.initial(ticks[0].p, c), c.origin, ticks[0].p, ticks[0].ms, "leveraged", 0
    sig = Signal(ticks[0].p, c)
    peak_eq, peak_p, global_peak = a.equity(ticks[0].p), ticks[0].p, a.equity(ticks[0].p)
    armed = False
    up_count = down_count = 0
    cooldown = c.cooldown_ticks if seeded else 0
    max_dd = 0.0
    min_buffer = a.buffer(ticks[0].p)
    max_lev = a.lev(ticks[0].p)
    events = []
    bases = [{"regime": rid, "time": iso(basis_ms), "price": basis_p, "equity": basis_eq, "seeded": seeded}]

    for t in ticks[1:]:
        if a.liquidated:
            break
        sig.update(t.p, c)
        if a.buffer(t.p) <= 0:
            before = a.equity(t.p)
            a.liquidate(t.p)
            events.append({"type": "liquidation", "regime": rid, "time": iso(t.ms), "price": t.p, "equity_before": before})
            break
        e = a.equity(t.p)
        global_peak = max(global_peak, e)
        max_dd = max(max_dd, (global_peak - e) / global_peak if global_peak else 0)
        min_buffer = min(min_buffer, a.buffer(t.p))
        max_lev = max(max_lev, a.lev(t.p))

        if phase == "leveraged":
            peak_eq, peak_p = max(peak_eq, e), max(peak_p, t.p)
            down_count = down_count + 1 if sig.down() else 0
            armed = armed or peak_eq >= basis_eq * (1 + c.activation_return)
            reason = None
            if a.buffer_ratio(t.p) <= c.emergency_buffer:
                reason = "emergency_buffer"
            elif armed:
                q2, close_fee, after = a.project(1.0, t.p)
                preserved = after >= basis_eq
                reversal = t.p <= peak_p * (1 - c.price_reversal)
                trail = max(basis_eq, peak_eq * (1 - c.equity_giveback))
                if preserved and reversal:
                    reason = "confirmed_price_reversal"
                elif preserved and e <= trail:
                    reason = "equity_giveback"
                elif preserved and down_count >= c.trend_break_ticks:
                    reason = "confirmed_trend_break"
            if reason:
                old_eq, old_q, old_lev, old_basis = e, a.qty, a.lev(t.p), basis_eq
                adj = a.adjust(1.0, t.p)
                basis_eq, basis_p, basis_ms = a.normalize(t.p), t.p, t.ms
                rid += 1
                phase, cooldown, armed, up_count, down_count = "basis", c.cooldown_ticks, False, 0, 0
                peak_eq, peak_p = basis_eq, t.p
                event = {"type": "closure_basis", "regime": rid, "time": iso(t.ms), "ts_ms": t.ms,
                         "price": t.p, "reason": reason, "previous_basis_equity": old_basis,
                         "new_basis_equity": basis_eq, "basis_growth_pct": (basis_eq / old_basis - 1) * 100,
                         "quantity_before": old_q, "quantity_after": a.qty, "leverage_before": old_lev,
                         "leverage_after": a.lev(t.p), "fee": adj["fee"], "basis_preserved": basis_eq >= old_basis}
                events.append(event)
                bases.append({"regime": rid, "time": iso(t.ms), "price": basis_p, "equity": basis_eq, "seeded": False})
            continue

        cooldown = max(0, cooldown - 1)
        up_count = up_count + 1 if sig.up() else 0
        breakout_move = max(c.min_breakout, c.breakout_vol_mult * sig.sigma() * math.sqrt(c.vol_horizon))
        breakout_p = basis_p * (1 + breakout_move)
        if cooldown == 0 and up_count >= c.trend_confirm_ticks and t.p >= breakout_p:
            info = new_leverage(a, t.p, basis_eq, sig, c)
            if info["target"] >= c.min_reentry_lev:
                before_e, before_q, before_l = a.equity(t.p), a.qty, a.lev(t.p)
                adj = a.adjust(info["target"], t.p)
                if a.equity(t.p) + 1e-8 < basis_eq:
                    raise AssertionError("new leverage consumed the protected basis")
                phase, peak_eq, peak_p, armed, up_count, down_count = "leveraged", a.equity(t.p), t.p, False, 0, 0
                events.append({"type": "new_leverage_regime", "regime": rid, "time": iso(t.ms), "ts_ms": t.ms,
                               "price": t.p, "basis_price": basis_p, "basis_equity": basis_eq,
                               "breakout_price": breakout_p, "equity_before": before_e,
                               "equity_after": a.equity(t.p), "quantity_before": before_q,
                               "quantity_after": a.qty, "leverage_before": before_l,
                               "target_leverage": info["target"], "leverage_after": a.lev(t.p),
                               "fee": adj["fee"], **info})

    last = ticks[-1]
    final_e = a.equity(last.p)
    return {"status": "liquidated" if a.liquidated else "completed", "paper_only": True,
            "seeded_from_basis": seeded, "origin_capital": c.origin, "final_price": last.p,
            "final_equity": final_e, "return_on_origin_pct": (final_e / c.origin - 1) * 100,
            "final_quantity_btc": a.qty, "final_leverage": a.lev(last.p), "final_phase": phase,
            "current_basis": {"regime": rid, "time": iso(basis_ms), "price": basis_p,
                              "equity": basis_eq, "marked_equity": final_e},
            "closure_count": sum(x["type"] == "closure_basis" for x in events),
            "reentry_count": sum(x["type"] == "new_leverage_regime" for x in events),
            "fees_paid": a.fees, "maximum_drawdown_pct": max_dd * 100,
            "maximum_effective_leverage": max_lev, "minimum_liquidation_buffer": min_buffer,
            "ticks": len(ticks), "events": events, "bases": bases, "config": asdict(c)}


def self_test():
    prices = [100,100.02,100.05,100.1,100.2,100.19,100.2,100.3,100.4,100.5,100.6,100.8,101,101.2,101.4,101.6,101.8,102,102.2,102.1,102]
    ticks = [Tick(i * 1000, p, 1, str(i)) for i, p in enumerate(prices)]
    c = Cfg(fast_ticks=2, slow_ticks=4, trend_confirm_ticks=1, cooldown_ticks=1,
            min_breakout=0.001, breakout_vol_mult=0, vol_window=8, vol_horizon=4,
            vol_mult=1, min_adverse_move=0.01, max_reentry_lev=5)
    r = run(ticks, c)
    assert r["closure_count"] == 2 and r["reentry_count"] == 1
    assert all(x.get("basis_preserved", True) for x in r["events"])
    return r


def main():
    if os.getenv("SELF_TEST") == "1":
        print(json.dumps(self_test(), indent=2)); return
    ticks_path = Path(os.getenv("TICKS_PATH", "results/live_tick/live_ticks.csv"))
    output = Path(os.getenv("OUTPUT_PATH", "results/live_tick/recursive_closure.json"))
    seed_eq = float(os.environ["SEED_BASIS_EQUITY"]) if os.getenv("SEED_BASIS_EQUITY") else None
    seed_p = float(os.environ["SEED_BASIS_PRICE"]) if os.getenv("SEED_BASIS_PRICE") else None
    seed_ms = int(os.environ["SEED_BASIS_TS_MS"]) if os.getenv("SEED_BASIS_TS_MS") else None
    ticks = load_ticks(ticks_path, seed_ms)
    result = run(ticks, Cfg(), seed_eq, seed_p, seed_ms)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "source": "Kraken public BTC/USD trade ticks; simulated leverage; no orders",
               "market": {"ticks": len(ticks), "first_time": iso(ticks[0].ms), "last_time": iso(ticks[-1].ms),
                          "first_price": ticks[0].p, "last_price": ticks[-1].p,
                          "high": max(t.p for t in ticks), "low": min(t.p for t in ticks)},
               "strategy": result}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
