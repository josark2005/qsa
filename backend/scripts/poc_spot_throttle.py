from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "poc" / "spot_throttle"
OUT.mkdir(parents=True, exist_ok=True)

URL = "https://82.push2.eastmoney.com/api/qt/clist/get"
PARAMS = {
    "pn": "1",
    "pz": "100",
    "po": "1",
    "np": "1",
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": "2",
    "invt": "2",
    "fid": "f12",
    "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
    "fields": (
        "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
        "f20,f21,f23,f24,f25,f22,f11,f62,f128,f136,f115,f152"
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--delay", type=float, default=1.5)
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    rows: list[dict] = []
    total = None
    started = time.perf_counter()
    log = []
    try:
        for page in range(1, args.pages + 1):
            params = dict(PARAMS, pn=str(page))
            page_started = time.perf_counter()
            response = session.get(URL, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") or {}
            total = data.get("total")
            diff = data.get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            rows.extend(diff)
            item = {
                "page": page,
                "returned": len(diff),
                "elapsed": round(time.perf_counter() - page_started, 2),
            }
            log.append(item)
            print(item)
            if len(diff) < 100:
                break
            time.sleep(args.delay)
    except Exception as exc:
        print("ERROR:", type(exc).__name__, exc)
        log.append({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        elapsed = round(time.perf_counter() - started, 2)
        sample_path = OUT / "sample.json"
        sample_path.write_text(
            json.dumps(rows[:5], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result = {
            "pages_requested": args.pages,
            "delay_seconds": args.delay,
            "total_market": total,
            "rows": len(rows),
            "elapsed_seconds": elapsed,
            "log": log,
        }
        (OUT / "results.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if rows:
            pd.DataFrame(rows).to_parquet(OUT / "rows.parquet", index=False)


if __name__ == "__main__":
    main()
