#!/usr/bin/env python3
"""Stand-in for a real send channel.

The gate invokes senders as: <sender command...> "<text>"
This one just prints what it would have sent.
"""

import sys

text = " ".join(sys.argv[1:]).strip()
if not text:
      print("[mock-send] (empty)", file=sys.stderr)
      raise SystemExit(1)
  print(f"[mock-send] {text}")
