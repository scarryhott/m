#!/usr/bin/env python3
"""Corrected execution wrapper for btc_recursive_backtest.py.

Corrections applied before execution:
1. Overlay Binance daily contract/mark/funding archives for July 26-27, 2021.
2. Fail closed if the requested July 26 entry minute is unavailable.
3. Preserve cumulative fee/funding/turnover counters when accounts are cloned.
4. Bound reported drawdown at 100% near insolvency.

The strategy and parameter grid are otherwise unchanged.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = (HERE / "btc_recursive_backtest.py").read_text(encoding="utf-8")

needle = " a=kl(ks);b=kl(mk);f=fr(fs);tt=sorted(set(a)&set(b));"
overlay = """\n # The completed-month archives used above did not provide aligned rows for\n # the requested July 26-27, 2021 entry window. Overlay the daily archives;\n # dictionary assignment below gives these exact daily rows precedence.\n for od in [date(2021,7,26),date(2021,7,27)]:\n  oq=str(od)\n  ox=arc(f'{B}/daily/klines/{S}/{I}/{S}-{I}-{oq}.zip',cache/'override_k',True)\n  oy=arc(f'{B}/daily/markPriceKlines/{S}/{I}/{S}-{I}-{oq}.zip',cache/'override_m',True)\n  oz=arc(f'{B}/daily/fundingRate/{S}/{S}-fundingRate-{oq}.zip',cache/'override_f',False)\n  ks.append(ox);mk.append(oy)\n  if oz:fs.append(oz)\n a=kl(ks);b=kl(mk);f=fr(fs);tt=sorted(set(a)&set(b));"""
if needle not in SOURCE:
    raise RuntimeError("daily-overlay insertion point not found")
SOURCE = SOURCE.replace(needle, overlay, 1)

SOURCE = SOURCE.replace(
    "z=A(a.w,a.q,a.e);",
    "z=A(a.w,a.q,a.e,a.fees,a.fund,a.turn,a.dead);",
)
SOURCE = SOURCE.replace(
    "dd=max(dd,(gp-E)/gp if gp else 0);",
    "dd=min(1.0,max(dd,(gp-E)/gp if gp else 0));",
)

entry_needle = "full=m.sl(entry,m.t[-1]);tr=m.sl(entry,split-1);"
entry_replacement = """full=m.sl(entry,m.t[-1])\n if len(full.t)==0 or full.t[0]>entry+60000:\n  raise RuntimeError(f'requested entry unavailable: requested={iso(entry)} first={iso(full.t[0]) if len(full.t) else None}')\n tr=m.sl(entry,split-1);"""
if entry_needle not in SOURCE:
    raise RuntimeError("entry validation insertion point not found")
SOURCE = SOURCE.replace(entry_needle, entry_replacement, 1)

module = types.ModuleType("btc_recursive_backtest_corrected_runtime")
module.__file__ = str(HERE / "btc_recursive_backtest.py")
sys.modules[module.__name__] = module
exec(compile(SOURCE, module.__file__, "exec"), module.__dict__)

# Retain the exact constant-time rolling variance implementation from the
# previously verified fast runner.
from collections import deque
import math

class FastSig:
    def __init__(self,p,c):
        self.f=self.s=self.pf=self.pp=p
        self.r=deque();self.s1=0.0;self.s2=0.0
        self.af=2/(c.fast+1);self.asl=2/(c.slow+1);self.n=c.vw
    def up(self,p):
        self.pf=self.f;x=math.log(p/self.pp);self.pp=p
        if len(self.r)>=self.n:
            old=self.r.popleft();self.s1-=old;self.s2-=old*old
        self.r.append(x);self.s1+=x;self.s2+=x*x
        self.f=self.af*p+(1-self.af)*self.f
        self.s=self.asl*p+(1-self.asl)*self.s
        return self.f>self.s and self.f>=self.pf
    def sig(self):
        n=len(self.r)
        if n<3:return 0.0
        return math.sqrt(max(0.0,self.s2/n-(self.s1/n)**2))

module.Sig = FastSig

if __name__ == "__main__":
    module.main()
