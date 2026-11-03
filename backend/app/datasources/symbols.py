from __future__ import annotations


def normalize_code(code: str | int) -> str:
    return str(code).strip().zfill(6)


def exchange_for_code(code: str | int) -> str:
    code = normalize_code(code)
    if code.startswith("6"):
        return "SH"
    if code.startswith(("0", "3")):
        return "SZ"
    if code.startswith(("4", "8", "9")):
        return "BJ"
    raise ValueError(f"unsupported security code: {code}")


def canonical_symbol(code: str | int, exchange: str | None = None) -> str:
    code = normalize_code(code)
    exchange = (exchange or exchange_for_code(code)).upper()
    return f"{code}.{exchange}"


def parse_symbol(symbol: str) -> tuple[str, str]:
    if "." not in symbol:
        return normalize_code(symbol), exchange_for_code(symbol)
    code, exchange = symbol.split(".", 1)
    return normalize_code(code), exchange.upper()


def to_baostock_symbol(symbol: str) -> str:
    code, exchange = parse_symbol(symbol)
    if exchange == "BJ":
        raise ValueError("BaoStock does not support Beijing Stock Exchange symbols")
    return f"{exchange.lower()}.{code}"


def to_tencent_symbol(symbol: str) -> str:
    code, exchange = parse_symbol(symbol)
    return f"{exchange.lower()}{code}"


def from_baostock_symbol(value: str) -> str:
    exchange, code = value.split(".", 1)
    return canonical_symbol(code, exchange)


def board_for_code(code: str | int) -> str:
    code = normalize_code(code)
    if code.startswith("688"):
        return "star"
    if code.startswith(("300", "301")):
        return "gem"
    if code.startswith(("4", "8", "9")):
        return "bse"
    if code.startswith("6"):
        return "main"
    if code.startswith("0"):
        return "main"
    return "unknown"


def is_bse_code(code: str | int) -> bool:
    return normalize_code(code).startswith(("4", "8", "9"))


def is_bse_symbol(symbol: str) -> bool:
    _, exchange = parse_symbol(symbol)
    return exchange == "BJ"
