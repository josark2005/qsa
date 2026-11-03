from __future__ import annotations

import json
import time
from pathlib import Path

import akshare as ak
import requests

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "poc" / "eastmoney_bulk"
OUT.mkdir(parents=True, exist_ok=True)

URL = "https://82.push2.eastmoney.com/api/qt/clist/get"
PARAMS = {
    "pn": "1",
    "pz": "6000",
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

results: dict = {}


def test_bulk_spot() -> None:
    started = time.perf_counter()
    try:
        response = requests.get(
            URL,
            params=PARAMS,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        elapsed = round(time.perf_counter() - started, 3)
        item = {
            "status_code": response.status_code,
            "elapsed_seconds": elapsed,
            "content_length": len(response.content),
        }
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        diff = data.get("diff")
        if isinstance(diff, dict):
            diff = list(diff.values())
        item.update(
            {
                "total": data.get("total"),
                "returned": len(diff or []),
                "first": (diff or [None])[0],
            }
        )
        if diff:
            (OUT / "bulk_spot_sample.json").write_text(
                json.dumps(diff[:3], ensure_ascii=False, indent=2), encoding="utf-8"
            )
        results["bulk_spot"] = item
        print("bulk_spot:", json.dumps(item, ensure_ascii=False))
    except Exception as exc:
        results["bulk_spot"] = {
            "status": "error",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }
        print("bulk_spot error:", results["bulk_spot"])


def test_bse_history() -> None:
    code = "920000"
    started = time.perf_counter()
    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date="20250101",
            end_date="20261030",
            adjust="qfq",
        )
        item = {
            "rows": len(df),
            "columns": list(df.columns),
            "sample": json.loads(df.tail(2).to_json(orient="records", force_ascii=False)),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        df.to_parquet(OUT / "bse_920000.parquet", index=False)
        results["bse_history"] = item
        print("bse_history:", json.dumps(item, ensure_ascii=False, default=str))
    except Exception as exc:
        results["bse_history"] = {
            "status": "error",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }
        print("bse_history error:", results["bse_history"])


def test_zt_history() -> None:
    dates = ["20261029", "20260930", "20260630", "20260105", "20250603", "20250102", "20241008"]
    items = []
    for date in dates:
        started = time.perf_counter()
        try:
            df = ak.stock_zt_pool_em(date=date)
            items.append(
                {
                    "date": date,
                    "rows": len(df),
                    "columns": list(df.columns),
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                }
            )
            print(f"zt {date}: rows={len(df)} elapsed={time.perf_counter() - started:.2f}s")
        except Exception as exc:
            items.append(
                {
                    "date": date,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            print(f"zt {date}: error {type(exc).__name__}: {exc}")
            break
        time.sleep(8)
    results["zt_history"] = items


def main() -> None:
    test_bulk_spot()
    time.sleep(10)
    test_bse_history()
    time.sleep(10)
    test_zt_history()
    (OUT / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("saved:", OUT / "results.json")


if __name__ == "__main__":
    main()
