import re

from walletledger.errors import InvalidRequest

# Minor-unit exponents (ISO 4217) for the currencies this ledger accepts.
CURRENCY_EXPONENTS: dict[str, int] = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "BRL": 2,
    "CAD": 2,
    "AUD": 2,
    "CHF": 2,
    "MXN": 2,
    "ARS": 2,
    "JPY": 0,
    "KRW": 0,
    "CLP": 0,
    "BHD": 3,
    "KWD": 3,
}

# Far below the bigint limit, so sums of many entries can never overflow.
MAX_AMOUNT_MINOR = 10**15

_AMOUNT_TEXT = re.compile(r"^(\d{1,16})(?:\.(\d+))?$")


def normalize_currency(code: str) -> str:
    currency = code.strip().upper() if isinstance(code, str) else ""
    if currency not in CURRENCY_EXPONENTS:
        supported = ", ".join(sorted(CURRENCY_EXPONENTS))
        raise InvalidRequest(f"unsupported currency {code!r}; supported: {supported}")
    return currency


def parse_amount(text: str, currency: str) -> int:
    """Turn a positive decimal string such as "10.50" into minor units (1050).

    Amounts are text, never floats, and may not have more decimals than the
    currency allows, so nothing is ever silently rounded.
    """
    exponent = CURRENCY_EXPONENTS[normalize_currency(currency)]
    match = _AMOUNT_TEXT.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise InvalidRequest(
            f"{text!r} is not a valid amount; use digits with an optional decimal part"
        )

    whole, fraction = match.group(1), match.group(2) or ""
    if len(fraction) > exponent:
        raise InvalidRequest(f"{currency} amounts allow at most {exponent} decimal places")

    amount_minor = int(whole + fraction.ljust(exponent, "0"))
    if amount_minor <= 0:
        raise InvalidRequest("amount must be greater than zero")
    if amount_minor > MAX_AMOUNT_MINOR:
        raise InvalidRequest("amount is too large")
    return amount_minor


def format_amount(amount_minor: int, currency: str) -> str:
    exponent = CURRENCY_EXPONENTS[normalize_currency(currency)]
    sign = "-" if amount_minor < 0 else ""
    digits = str(abs(amount_minor)).rjust(exponent + 1, "0")
    if exponent == 0:
        return f"{sign}{digits}"
    return f"{sign}{digits[:-exponent]}.{digits[-exponent:]}"
