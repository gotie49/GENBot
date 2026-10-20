"""Docker HEALTHCHECK: healthy while the monitor loop keeps refreshing its heartbeat file.

Exit 0 = healthy, 1 = unhealthy. Usage: ``python -m statustw_bot.healthcheck``.
"""

from __future__ import annotations

import os
import sys
import time


def main() -> int:
    path = os.environ.get("HEARTBEAT_FILE")
    if not path:
        return 0  # heartbeat disabled
    interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "60") or 60)
    max_age = 3 * interval + 120  # a slow cycle (rate limit pause, bulk download) is still fine
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        print("heartbeat file missing", file=sys.stderr)
        return 1
    if age > max_age:
        print(f"heartbeat stale ({age:.0f}s > {max_age}s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
