from __future__ import annotations

import json
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import baostock as bs
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "poc" / "baostock_validation"
OUT.mkdir(parents=True, exist_ok=True)

TRADE_DATE = "2026-10-30"


def fetch(rs) -> pd.DataFrame:
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if rs.error_code != "0":
        raise RuntimeError(f"{rs.error_code}: {rs.error_msg}")
    return pd.DataFrame(rows, columns=rs.fields)


def limit_price(preclose: float, pct: float) -> float:
    return float(
        (Decimal(str(preclose)) * (Decimal("1") + Decimal(str(pct)))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def board_rule(code: str, is_st: str) -> tuple[str, float]:
    if code.startswith(("sh.688", "sz.300", "sz.301")):
        return "star_gem", 0.20
    if code.startswith(("bj.",)):
        return "bse", 0.30
    if is_st == "1":
        return "st_main", 0.05
    return "main", 0.10


def sample_codes() -> list[str]:
    zt_files = sorted((ROOT / "data" / "poc").glob("20261102_*/zt_pool_recent.parquet"))
    if zt_files:
        df = pd.read_parquet(zt_files[0])
        codes = df["代码"].astype(str).tolist()
    else:
        codes = ["603703", "002365", "300030", "688045"]
    wanted = []
    for prefix in ("603", "002", "300", "688"):
        for code in codes:
            if code.startswith(prefix):
                wanted.append(code)
                break
    return wanted


def main() -> None:
    lg = bs.login()
    if lg.error_code != "0":
        raise SystemExit(f"login failed: {lg.error_msg}")

    results: dict = {"trade_date": TRADE_DATE, "limits": []}
    try:
        # 1. Limit price validation from actual limit-up pool
        for raw_code in sample_codes():
            exchange = "sh" if raw_code.startswith("6") else "sz"
            code = f"{exchange}.{raw_code}"
            rs = bs.query_history_k_data_plus(
                code,
                "date,code,open,high,low,close,preclose,isST,tradestatus",
                start_date="2026-10-28",
                end_date=TRADE_DATE,
                frequency="d",
                adjustflag="3",
            )
            df = fetch(rs)
            if df.empty:
                results["limits"].append({"code": code, "error": "no data"})
                continue
            row = df.iloc[-1].to_dict()
            board, pct = board_rule(code, str(row["isST"]))
            expected = limit_price(float(row["preclose"]), pct)
            results["limits"].append(
                {
                    "code": code,
                    "board": board,
                    "preclose": float(row["preclose"]),
                    "close": float(row["close"]),
                    "high": float(row["high"]),
                    "is_st": row["isST"],
                    "limit_pct": pct,
                    "expected_limit_price": expected,
                    "close_is_limit": abs(float(row["close"]) - expected) < 1e-9,
                    "high_touched_limit": abs(float(row["high"]) - expected) < 1e-9,
                }
            )

        # 2. ETF adjustment factors
        rs = bs.query_adjust_factor(
            code="sh.510300", start_date="2024-01-01", end_date="2026-10-30"
        )
        etf_adj = fetch(rs)
        etf_adj.to_parquet(OUT / "etf_adjust.parquet", index=False)
        results["etf_adjust_rows"] = len(etf_adj)
        results["etf_adjust_columns"] = list(etf_adj.columns)
        results["etf_adjust_sample"] = etf_adj.head(3).to_dict("records")

        # 3. Delisted stock history
        rs = bs.query_history_k_data_plus(
            "sz.000004",
            "date,code,open,high,low,close,preclose,volume,amount,tradestatus,isST",
            start_date="2026-06-01",
            end_date="2026-07-14",
            frequency="d",
            adjustflag="3",
        )
        delisted = fetch(rs)
        delisted.to_parquet(OUT / "delisted_000004.parquet", index=False)
        results["delisted_000004_rows"] = len(delisted)
        results["delisted_000004_last"] = (
            delisted.tail(2).to_dict("records") if not delisted.empty else []
        )

        # 4. All securities basic info without code filter
        rs = bs.query_stock_basic()
        all_basic = fetch(rs)
        all_basic.to_parquet(OUT / "all_basic.parquet", index=False)
        results["all_basic_rows"] = len(all_basic)
        results["all_basic_columns"] = list(all_basic.columns)
        if not all_basic.empty:
            results["all_basic_type_counts"] = (
                all_basic["type"].value_counts().to_dict()
            )

        # 5. Industry classification if available
        if hasattr(bs, "query_stock_industry"):
            rs = bs.query_stock_industry()
            industry = fetch(rs)
            industry.to_parquet(OUT / "industry.parquet", index=False)
            results["industry_rows"] = len(industry)
            results["industry_columns"] = list(industry.columns)
        else:
            results["industry_rows"] = None

        # 6. Backfill throughput benchmark
        basic = all_basic
        stock_codes = basic.loc[basic["type"] == "1", "code"].head(10).tolist()
        started = time.perf_counter()
        total_rows = 0
        for code in stock_codes:
            rs = bs.query_history_k_data_plus(
                code,
                "date,code,open,high,low,close,preclose,volume,amount,turn,"
                "tradestatus,pctChg,isST,peTTM,pbMRQ",
                start_date="2021-01-01",
                end_date="2026-10-30",
                frequency="d",
                adjustflag="3",
            )
            total_rows += len(fetch(rs))
        elapsed = time.perf_counter() - started
        results["backfill_benchmark"] = {
            "stocks": len(stock_codes),
            "rows": total_rows,
            "seconds": round(elapsed, 3),
            "seconds_per_stock": round(elapsed / max(len(stock_codes), 1), 3),
            "estimated_hours_5223_stocks": round(
                elapsed / max(len(stock_codes), 1) * 5223 / 3600, 2
            ),
        }
    finally:
        bs.logout()

    (OUT / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
