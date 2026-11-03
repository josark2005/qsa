from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import requests

from app.core.parsing import parse_date
from app.datasources.base import RateLimiter, retry_call
from app.datasources.symbols import to_tencent_symbol

logger = logging.getLogger(__name__)

QUOTE_URL = "https://qt.gtimg.cn/q="
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
KLINE_RAW_URL = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
KLINE_PAGE_SIZE = 640

FIELD_POSITIONS = {
    "name": 1,
    "code": 2,
    "price": 3,
    "preclose": 4,
    "open": 5,
    "volume_hand": 6,
    "quote_time": 30,
    "change": 31,
    "pct_chg": 32,
    "high": 33,
    "low": 34,
    "amount_wan": 37,
    "turnover_rate": 38,
    "pe_ttm": 39,
    "amplitude": 43,
    "float_mv_yi": 44,
    "total_mv_yi": 45,
    "pb": 46,
    "up_limit": 47,
    "down_limit": 48,
    "volume_ratio": 49,
}

NUMERIC_COLUMNS = [
    "price",
    "preclose",
    "open",
    "high",
    "low",
    "change",
    "pct_chg",
    "amount_wan",
    "turnover_rate",
    "pe_ttm",
    "amplitude",
    "float_mv_yi",
    "total_mv_yi",
    "pb",
    "up_limit",
    "down_limit",
    "volume_ratio",
    "volume_hand",
]


@dataclass
class TencentClient:
    batch_size: int = 500
    delay_seconds: float = 1.0
    timeout: int = 30

    def __post_init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0"})
        self._limiter = RateLimiter(self.delay_seconds)

    def close(self) -> None:
        self._session.close()

    def fetch_quotes(self, symbols: list[str]) -> pd.DataFrame:
        rows: list[dict] = []
        for start in range(0, len(symbols), self.batch_size):
            batch = symbols[start : start + self.batch_size]
            tx_symbols = [to_tencent_symbol(symbol) for symbol in batch]
            text = retry_call(
                lambda: self._fetch_text(tx_symbols),
                attempts=3,
                base_delay=2.0,
                label="tencent_quotes",
            )
            parsed = [row for line in text.splitlines() if (row := _parse_line(line))]
            rows.extend(parsed)
            if len(parsed) < len(batch):
                logger.warning(
                    "tencent batch returned %s/%s rows", len(parsed), len(batch)
                )
            self._limiter.wait()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        for column in NUMERIC_COLUMNS:
            df[column] = pd.to_numeric(df[column], errors="coerce")
        df["volume"] = (df["volume_hand"] * 100).astype("Int64")
        df["amount"] = df["amount_wan"] * 10000
        df["is_suspended"] = (df["volume"].fillna(0) == 0) | (df["price"] <= 0)
        df["source"] = "tencent"
        return df

    def _fetch_text(self, tx_symbols: list[str]) -> str:
        url = QUOTE_URL + ",".join(tx_symbols)
        response = self._session.get(url, timeout=self.timeout)
        response.raise_for_status()
        response.encoding = "gbk"
        return response.text


@dataclass
class TencentKlineClient:
    delay_seconds: float = 0.15
    timeout: int = 30

    def __post_init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0"})
        self._limiter = RateLimiter(self.delay_seconds)

    def close(self) -> None:
        self._session.close()

    def fetch_history(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        *,
        adjust: str = "",
    ) -> pd.DataFrame:
        tx_symbol = to_tencent_symbol(symbol)
        key = "day" if not adjust else f"{adjust}day"
        cursor_end = end_date
        collected: dict[str, list[str]] = {}
        for _ in range(30):
            params = {
                "param": (
                    f"{tx_symbol},day,{start_date.isoformat()},"
                    f"{cursor_end.isoformat()},{KLINE_PAGE_SIZE},{adjust}"
                )
            }
            url = KLINE_RAW_URL if not adjust else KLINE_URL
            payload = retry_call(
                lambda: self._fetch_json(url, params),
                attempts=3,
                base_delay=1.5,
                label=f"tencent_kline:{symbol}:{adjust or 'raw'}",
            )
            data = payload.get("data")
            if not isinstance(data, dict):
                break
            node = data.get(tx_symbol) or {}
            rows = node.get(key) or []
            if not rows:
                break
            for row in rows:
                if len(row) < 6:
                    continue
                collected[str(row[0])] = row
            first_date = parse_date(rows[0][0])
            if first_date is None or first_date <= start_date:
                break
            cursor_end = first_date - timedelta(days=1)
            self._limiter.wait()
        if not collected:
            return pd.DataFrame()
        data = {
            "trade_date": [parse_date(value[0]) for value in collected.values()],
            "open": [value[1] for value in collected.values()],
            "close": [value[2] for value in collected.values()],
            "high": [value[3] for value in collected.values()],
            "low": [value[4] for value in collected.values()],
            "volume_hand": [value[5] for value in collected.values()],
        }
        df = pd.DataFrame(data)
        df = df.dropna(subset=["trade_date"]).sort_values("trade_date")
        df = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)]
        return df.reset_index(drop=True)

    def fetch_raw_and_adj(
        self, symbol: str, start_date: date, end_date: date
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        raw = self.fetch_history(symbol, start_date, end_date, adjust="")
        self._limiter.wait()
        adjusted = self.fetch_history(symbol, start_date, end_date, adjust="hfq")
        return raw, adjusted

    def _fetch_json(self, url: str, params: dict) -> dict:
        response = self._session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (0, "0", None):
            raise RuntimeError(f"tencent kline error: {payload.get('msg')}")
        return payload


def _parse_line(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("v_") or '="' not in line:
        return None
    raw_symbol = line.split("=", 1)[0][2:]
    payload = line.split('="', 1)[1].rsplit('"', 1)[0]
    fields = payload.split("~")
    if len(fields) < 49:
        return None
    row: dict[str, object] = {"tencent_symbol": raw_symbol}
    for column, index in FIELD_POSITIONS.items():
        row[column] = fields[index]
    row["code"] = str(row["code"]).zfill(6)
    exchange = raw_symbol[:2].upper()
    row["symbol"] = f"{row['code']}.{exchange}"
    return row
