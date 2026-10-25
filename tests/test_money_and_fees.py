import pytest

from walletledger.errors import InvalidRequest
from walletledger.fees import FeePolicy
from walletledger.money import MAX_AMOUNT_MINOR, format_amount, normalize_currency, parse_amount


class TestParseAmount:
    @pytest.mark.parametrize(
        ("text", "currency", "expected"),
        [
            ("10", "USD", 1000),
            ("10.5", "USD", 1050),
            ("10.50", "USD", 1050),
            ("0.01", "USD", 1),
            ("1234567.89", "EUR", 123456789),
            ("007.10", "USD", 710),
            ("500", "JPY", 500),
            ("1.234", "BHD", 1234),
            ("10000000000000", "USD", MAX_AMOUNT_MINOR),
        ],
    )
    def test_turns_decimal_text_into_minor_units(self, text, currency, expected):
        assert parse_amount(text, currency) == expected

    @pytest.mark.parametrize(
        "text",
        ["", "abc", "-5", "+5", "1e3", "1,000", " 5", "5 ", "1.", ".5", "0", "0.00", "10.001"],
    )
    def test_rejects_malformed_text_zero_and_extra_decimals(self, text):
        with pytest.raises(InvalidRequest):
            parse_amount(text, "USD")

    def test_rejects_decimals_for_a_currency_without_minor_units(self):
        with pytest.raises(InvalidRequest, match="at most 0 decimal places"):
            parse_amount("10.5", "JPY")

    def test_rejects_amounts_above_the_supported_maximum(self):
        with pytest.raises(InvalidRequest, match="too large"):
            parse_amount("10000000000001", "USD")

    @pytest.mark.parametrize("not_text", [None, 10, 10.5])
    def test_rejects_values_that_are_not_text(self, not_text):
        with pytest.raises(InvalidRequest):
            parse_amount(not_text, "USD")


class TestFormatAmount:
    @pytest.mark.parametrize(
        ("minor", "currency", "expected"),
        [
            (1050, "USD", "10.50"),
            (5, "USD", "0.05"),
            (0, "USD", "0.00"),
            (-5, "USD", "-0.05"),
            (-123456, "EUR", "-1234.56"),
            (500, "JPY", "500"),
            (1234, "BHD", "1.234"),
            (7, "BHD", "0.007"),
        ],
    )
    def test_formats_minor_units_with_the_currency_exponent(self, minor, currency, expected):
        assert format_amount(minor, currency) == expected

    @pytest.mark.parametrize("currency", ["USD", "JPY", "BHD"])
    @pytest.mark.parametrize("minor", [1, 9, 10, 99, 100, 12345, 10**12])
    def test_formatting_then_parsing_gives_the_same_amount(self, minor, currency):
        assert parse_amount(format_amount(minor, currency), currency) == minor


class TestNormalizeCurrency:
    def test_accepts_any_case_and_surrounding_spaces(self):
        assert normalize_currency(" usd ") == "USD"

    @pytest.mark.parametrize("code", ["XXX", "US", "", "dollar", None])
    def test_rejects_unsupported_codes(self, code):
        with pytest.raises(InvalidRequest, match="unsupported currency"):
            normalize_currency(code)


class TestFeePolicy:
    def test_charges_nothing_by_default(self):
        assert FeePolicy().fee_for(1_000_000) == 0

    def test_charges_a_percentage_in_basis_points(self):
        assert FeePolicy(basis_points=150).fee_for(10_000) == 150

    def test_rounds_the_percentage_up(self):
        assert FeePolicy(basis_points=100).fee_for(333) == 4

    def test_never_charges_less_than_the_minimum(self):
        assert FeePolicy(basis_points=100, minimum_minor=50).fee_for(1_000) == 50

    def test_a_minimum_alone_is_a_flat_fee(self):
        assert FeePolicy(minimum_minor=25).fee_for(1_000_000) == 25

    @pytest.mark.parametrize("basis_points", [-1, 10_001])
    def test_rejects_basis_points_outside_zero_to_ten_thousand(self, basis_points):
        with pytest.raises(InvalidRequest):
            FeePolicy(basis_points=basis_points)

    def test_rejects_a_negative_minimum(self):
        with pytest.raises(InvalidRequest):
            FeePolicy(minimum_minor=-1)
