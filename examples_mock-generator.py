#!/usr/bin/env python3
"""Stand-in for a real generation model.

The gate invokes generators as:  <generator command...> "<prompt>"
Print one line of text, or "--" to decline this round.
"""
import sys

prompt = " ".join(sys.argv[1:]).strip()
if not prompt:
    print("--")
    raise SystemExit(0)
print("[mock] 我记着这事，回头说给你听。")
