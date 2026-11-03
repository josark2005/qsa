from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "poc" / "tencent_bulk"
OUT.mkdir(parents=True, exist_ok=True)


def to_tx_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8", "9")):
        return f"bj{code}"
    return f"sz{code}"


def parse_response(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("v_") or '="' not in line:
            continue
        symbol = line.split("=", 1)[0][2:]
        payload = line.split('="', 1)[1].rsplit('"', 1)[0]
        fields = payload.split("~")
        if len(fields) < 48:
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": fields[1],
                "code": fields[2],
                "price": fields[3],
                "preclose": fields[4],
                "open": fields[5],
                "volume_hand": fields[6],
                "time": fields[29],
                "change": fields[30],
                "change_pct": fields[31],
                "high": fields[32],
                "low": fields[33],
                "amount": fields[36],
                "turnover": fields[37],
                "pe_ttm": fields[38],
                "high2": fields[40],
                "low2": fields[41],
                "amplitude": fields[42],
                "float_mv": fields[43],
                "total_mv": fields[44],
                "pb": fields[45],
                "limit_up": fields[46],
                "limit_down": fields[47],
                "volume_ratio": fields[48] if len(fields) > 48 else None,
                "raw_fields": len(fields),
            }
        )
    return rows


def main() -> None:
    stock_info_path = None
    for path in sorted((ROOT / "data" / "poc").glob("20261102_*/stock_info.parquet")):
        stock_info_path = path
        break
    if stock_info_path is None:
        raise SystemExit("stock_info.parquet not found, run poc_data_sources first")
    universe = pd.read_parquet(stock_info_path)
    codes = universe["code"].astype(str).str.zfill(6).tolist()
    # Spread the sample across markets: take BSE plus SH/SZ.
    bse = [c for c in codes if c.startswith("92")][:150]
    sh = [c for c in codes if c.startswith("6")][:200]
    sz = [c for c in codes if c.startswith(("0", "3"))][:200]
    sample = bse + sh + sz

    results = {}
    for label, batch in (("batch_500", sample[:500]), ("batch_1000", sample)):
        symbols = [to_tx_symbol(c) for c in batch]
        url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
        started = time.perf_counter()
        try:
            response = requests.get(
                url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}
            )
            response.encoding = "gbk"
            elapsed = round(time.perf_counter() - started, 2)
            rows = parse_response(response.text)
            df = pd.DataFrame(rows)
            if not df.empty:
                df.to_parquet(OUT / f"{label}.parquet", index=False)
            item = {
                "requested": len(batch),
                "status_code": response.status_code,
                "response_bytes": len(response.content),
                "url_chars": len(url),
                "returned": len(rows),
                "elapsed_seconds": elapsed,
                "columns": list(df.columns) if not df.empty else [],
            }
            if not df.empty:
                bse_rows = df[df["symbol"].str.startswith("bj")]
                item["bse_requested"] = len(bse)
                item["bse_returned"] = len(bse_rows)
                item["sample"] = df.head(3).to_dict("records")
                item["bse_sample"] = bse_rows.head(3).to_dict("records")
                if label == "batch_500":
                    item["limit_up_nonempty"] = int((df["limit_up"] != "").sum())
                    item["limit_down_nonempty"] = int((df["limit_down"] != "").sum())
            results[label] = item
            print(label, json.dumps(item, ensure_ascii=False, default=str)[:2000])
        except Exception as exc:
            results[label] = {"error": f"{type(exc).__name__}: {exc}"}
            print(label, "ERROR", results[label])
        time.sleep(2)

    (OUT / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
