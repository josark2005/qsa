from __future__ import annotations

import logging
from dataclasses import dataclass

import akshare as ak
import pandas as pd

from app.core.parsing import parse_date, to_float, to_int
from app.datasources.base import RateLimiter, retry_call

logger = logging.getLogger(__name__)


@dataclass
class FinancialBundle:
    period: str
    yjbb: pd.DataFrame
    zcfz: pd.DataFrame
    lrb: pd.DataFrame
    xjll: pd.DataFrame


class AkShareClient:
    def __init__(self, delay_seconds: float = 0.6) -> None:
        self._limiter = RateLimiter(delay_seconds)

    def fetch_stock_list(self) -> pd.DataFrame:
        df = retry_call(
            ak.stock_info_a_code_name, attempts=3, base_delay=2, label="ak_stock_list"
        )
        df = df.copy()
        df["code"] = df["code"].astype(str).str.zfill(6)
        df["name"] = df["name"].astype(str)
        return df

    def fetch_financial_bundle(self, period: str) -> FinancialBundle:
        def fetch(func, label: str, required: bool) -> pd.DataFrame:
            try:
                self._limiter.wait()
                return retry_call(
                    lambda: func(date=period),
                    attempts=2,
                    base_delay=3,
                    label=label,
                )
            except Exception:
                if required:
                    raise
                logger.exception("%s failed for period %s", label, period)
                return pd.DataFrame()

        yjbb = fetch(ak.stock_yjbb_em, "ak_yjbb", True)
        zcfz = fetch(ak.stock_zcfz_em, "ak_zcfz", False)
        lrb = fetch(ak.stock_lrb_em, "ak_lrb", False)
        xjll = fetch(ak.stock_xjll_em, "ak_xjll", False)
        return FinancialBundle(period=period, yjbb=yjbb, zcfz=zcfz, lrb=lrb, xjll=xjll)

    def fetch_shareholders(self, period: str = "最新") -> pd.DataFrame:
        def call() -> pd.DataFrame:
            self._limiter.wait()
            return ak.stock_zh_a_gdhs(symbol=period)

        raw = retry_call(
            call,
            attempts=2,
            base_delay=3,
            label=f"ak_shareholders:{period}",
        )
        if raw is None or raw.empty:
            return pd.DataFrame()
        df = pd.DataFrame(
            {
                "code": raw["代码"].astype(str).str.zfill(6),
                "name": raw["名称"].astype(str),
                "period_end": raw["股东户数统计截止日-本次"].map(parse_date),
                "announce_date": raw["公告日期"].map(parse_date),
                "holder_num": raw["股东户数-本次"].map(to_int),
                "holder_num_prev": raw["股东户数-上次"].map(to_int),
                "holder_change": raw["股东户数-增减"].map(to_int),
                "holder_change_pct": raw["股东户数-增减比例"].map(to_float),
                "avg_holding_value": raw["户均持股市值"].map(to_float),
                "avg_holding_shares": raw["户均持股数量"].map(to_float),
                "total_mv": raw["总市值"].map(to_float),
                "total_shares": raw["总股本"].map(to_float),
            }
        )
        return df.dropna(subset=["period_end", "announce_date"])

    def fetch_bse_history(
        self, code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        def fetch(adjust: str) -> pd.DataFrame:
            self._limiter.wait()
            return retry_call(
                lambda: ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust,
                ),
                attempts=2,
                base_delay=5,
                label=f"ak_bse_history:{code}:{adjust or 'raw'}",
            )

        raw = fetch("")
        if raw is None or raw.empty:
            return pd.DataFrame()
        hfq = fetch("hfq")
        raw = raw.rename(
            columns={
                "日期": "trade_date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume_hand",
                "成交额": "amount",
                "涨跌幅": "pct_chg",
                "换手率": "turnover_rate",
            }
        )
        raw["trade_date"] = raw["trade_date"].map(parse_date)
        if hfq is None or hfq.empty:
            raw["close_adj"] = None
            return raw
        hfq = hfq.rename(columns={"日期": "trade_date", "收盘": "close_adj"})
        hfq["trade_date"] = hfq["trade_date"].map(parse_date)
        return raw.merge(
            hfq[["trade_date", "close_adj"]], on="trade_date", how="left"
        )
