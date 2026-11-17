"""Wait for HiveServer2, verify HiveQL, then initialize the app's Hive schema."""

import argparse
import os
import socket
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    args = parser.parse_args()
    host = os.environ.get("HIVE_HOST", "localhost")
    port = int(os.environ.get("HIVE_PORT", "10000"))
    deadline = time.monotonic() + args.timeout_seconds
    last_error = "HiveServer2 has not opened its port yet."
    last_report = 0.0
    print(f"Waiting for HiveServer2 at {host}:{port}; readiness requires HiveQL SELECT 1.", flush=True)

    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                with closing(app.connect()) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT 1")
                    result = cursor.fetchone()
                    if not result or int(result[0]) != 1:
                        raise RuntimeError("HiveQL SELECT 1 returned an unexpected result.")
                    app.ensure_schema(cursor)
                print("HiveQL SELECT 1 succeeded; Hive database and application tables are initialized.", flush=True)
                return 0
        except Exception as exc:  # Hive may accept TCP before HiveServer2 can serve queries.
            last_error = str(exc)
        now = time.monotonic()
        if now - last_report >= 10:
            print(f"Hive is still starting; last check: {last_error[:180]}", flush=True)
            last_report = now
        time.sleep(2)

    print(f"Hive did not become query-ready within {args.timeout_seconds} seconds. Last check: {last_error[:240]}", file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
