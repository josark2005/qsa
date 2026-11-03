import inspect

import akshare as ak

for name in ("stock_zh_a_spot_em", "fund_etf_spot_em", "stock_info_a_code_name"):
    print(f"\n===== {name} =====")
    print(inspect.getsource(getattr(ak, name)))
