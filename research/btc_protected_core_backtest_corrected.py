#!/usr/bin/env python3
"""Correct the protected-core overlay starting-value accounting before execution."""
from __future__ import annotations

import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = (HERE / "btc_protected_core_backtest.py").read_text(encoding="utf-8")

needle = "    first_price = float(m.o[start_index])\n    signal = FastSig(first_price, cfg)"
replacement = "    first_price = float(m.o[start_index])\n    initial_core_quantity = core_quantity\n    initial_overlay_cash = overlay_cash\n    signal = FastSig(first_price, cfg)"
if needle not in SOURCE:
    raise RuntimeError("initial portfolio insertion point not found")
SOURCE = SOURCE.replace(needle, replacement, 1)

needle = "    start_value = core_quantity * first_price + overlay_cash\n"
replacement = "    start_value = initial_core_quantity * first_price + initial_overlay_cash\n"
if needle not in SOURCE:
    raise RuntimeError("starting-value replacement point not found")
SOURCE = SOURCE.replace(needle, replacement, 1)

module = types.ModuleType("btc_protected_core_backtest_corrected_runtime")
module.__file__ = str(HERE / "btc_protected_core_backtest.py")
sys.modules[module.__name__] = module
exec(compile(SOURCE, module.__file__, "exec"), module.__dict__)

if __name__ == "__main__":
    module.main()
