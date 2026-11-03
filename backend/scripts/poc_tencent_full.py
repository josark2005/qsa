from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "poc" / "tencent_full"
OUT.mkdir(parents=True, exist_ok=True)

BATCH_SIZE = 500
DELAY_SECONDS = 1.0
COLUMNS = {
    "name": 1,
    "code": 2,
    "price": 3,
    "preclose": 4,
    "open": 5,
    "volume_hand": 6,
    "time": 30,
    "change": 31,
    "change_pct": 32,
    "high": 33,
    "low": 34,
    "amount_yuan": 35,
    "amount_wan": 37,
    "turnover": 38,
    "pe_ttm": 39,
    "amplitude": 43,
    "float_mv_yi": 44,
    "total_mv_yi": 45,
    "pb": 46,
    "limit_up": 47,
    "limit_down": 48,
    "volume_ratio": 49,
}


def to_tx_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8", "9")):
        return f"bj{code}"
    return f"sz{code}"


def parse_line(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("v_") or '="' not in line:
        return None
    symbol = line.split("=", 1)[0][2:]
    payload = line.split('="', 1)[1].rsplit('"', 1)[0]
    fields = payload.split("~")
    if len(fields) < 49:
        return None
    row = {"symbol": symbol, "raw_fields": len(fields)}
    for name, index in COLUMNS.items():
        row[name] = fields[index]
    return row


def main() -> None:
    stock_info_path = sorted(
        (ROOT / "data" / "poc").glob("20261102_*/stock_info.parquet")
    )[0]
    universe = pd.read_parquet(stock_info_path)
    codes = universe["code"].astype(str).str.zfill(6).tolist()
    batches = [codes[i : i + BATCH_SIZE] for i in range(0, len(codes), BATCH_SIZE)]

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    rows: list[dict] = []
    logs = []
    started = time.perf_counter()
    for index, batch in enumerate(batches, start=1):
        url = "https://qt.gtimg.cn/q=" + ",".join(to_tx_symbol(c) for c in batch)
        batch_started = time.perf_counter()
        response = session.get(url, timeout=30)
        response.raise_for_status()
        response.encoding = "gbk"
        parsed = [r for line in response.text.splitlines() if (r := parse_line(line))]
        rows.extend(parsed)
        item = {
            "batch": index,
            "requested": len(batch),
            "returned": len(parsed),
            "elapsed": round(time.perf_counter() - batch_started, 2),
            "bytes": len(response.content),
        }
        logs.append(item)
        print(item)
        time.sleep(DELAY_SECONDS)

    elapsed = round(time.perf_counter() - started, 2)
    df = pd.DataFrame(rows)
    df["code"] = df["code"].astype(str).str.zfill(6)
    numeric_cols = [
        "price",
        "preclose",
        "open",
        "high",
        "low",
        "turnover",
        "pe_ttm",
        "amplitude",
        "float_mv_yi",
        "total_mv_yi",
        "pb",
        "limit_up",
        "limit_down",
        "volume_ratio",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.to_parquet(OUT / "snapshot.parquet", index=False)

    missing = sorted(set(codes) - set(df["code"]))
    summary = {
        "universe": len(codes),
        "unique_returned": int(df["code"].nunique()),
        "rows": len(df),
        "missing_count": len(missing),
        "missing_sample": missing[:20],
        "bse_returned": int(df["code"].str.startswith("92").sum()),
        "bse_universe": int(sum(c.startswith("92") for c in codes)),
        "limit_up_nonempty": int(df["limit_up"].notna().sum()),
        "limit_down_nonempty": int(df["limit_down"].notna().sum()),
        "has_pe": int(df["pe_ttm"].notna().sum()),
        "has_pb": int(df["pb"].notna().sum()),
        "elapsed_seconds": elapsed,
        "batches": len(batches),
        "logs": logs,
    }

    # Cross-check preclose against the last known close from other sources.
    checks = {}
    bs_stock = sorted((ROOT / "data" / "poc").glob("20261102_*/bs_daily_stock.parquet"))
    if bs_stock:
        old = pd.read_parquet(bs_stock[0]).iloc[-1]
        current = df[df["code"] == "600000"].iloc[0]
        checks["600000"] = {
            "tencent_preclose": current["preclose"],
            "baostock_last_close": float(old["close"]),
            "baostock_last_date": old["date"],
        }
    bs_etf = sorted((ROOT / "data" / "poc").glob("20261102_*/bs_daily_etf.parquet"))
    if bs_etf:
        old = pd.read_parquet(bs_etf[0]).iloc[-1]
        current = df[df["code"] == "510300"].iloc[0]
        checks["510300"] = {
            "tencent_preclose": current["preclose"],
            "baostock_last_close": float(old["close"]),
            "baostock_last_date": old["date"],
        }
    bse = sorted((ROOT / "data" / "poc").glob("bse/920000.parquet"))
    if bse:
        old = pd.read_parquet(bse[0]).iloc[-1]
        current = df[df["code"] == "920000"].iloc[0]
        checks["920000"] = {
            "tencent_preclose": current["preclose"],
            "akshare_last_close": float(old["收盘"]),
            "akshare_last_date": str(old["日期"]),
        }
    summary["preclose_checks"] = checks
    summary["sample_bse"] = df[df["code"].str.startswith("92")].head(3).to_dict(
        "records"
    )
    summary["sample_main"] = df[df["code"] == "600000"].to_dict("records")

    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
