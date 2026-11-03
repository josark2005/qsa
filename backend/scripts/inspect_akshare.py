import inspect

import akshare as ak

print("akshare:", ak.__version__)

patterns = (
    "gdhs",
    "yjbb",
    "zcfz",
    "lrb",
    "xjll",
    "financial",
    "etf",
    "zt_pool",
    "spot_em",
    "trade_date",
    "stock_info",
)

names = sorted(name for name in dir(ak) if any(p in name for p in patterns))
for name in names:
    obj = getattr(ak, name)
    if not callable(obj):
        continue
    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):
        signature = "(?)"
    print(f"{name}{signature}")

focus = [
    "stock_zh_a_gdhs",
    "stock_zh_a_gdhs_detail_em",
    "stock_yjbb_em",
    "stock_zcfz_em",
    "stock_lrb_em",
    "stock_xjll_em",
    "stock_zt_pool_em",
    "stock_zt_pool_dtgc_em",
    "fund_etf_spot_em",
    "fund_etf_hist_em",
    "stock_zh_a_spot_em",
    "tool_trade_date_hist_sina",
]
print("\n--- docstrings ---")
for name in focus:
    obj = getattr(ak, name, None)
    if obj is None:
        print(f"\n{name}: NOT FOUND")
        continue
    print(f"\n{name}{inspect.signature(obj)}")
    print((inspect.getdoc(obj) or "").strip()[:1200])
