from dataclasses import dataclass

from walletledger.errors import InvalidRequest


@dataclass(frozen=True)
class FeePolicy:
    """Fee charged to the sender of a transfer, on top of the amount sent.

    The percentage is in basis points and rounds up, so rounding never costs
    the platform money. A minimum applies only when the policy charges at all.
    """

    basis_points: int = 0
    minimum_minor: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.basis_points <= 10_000:
            raise InvalidRequest("basis_points must be between 0 and 10000")
        if self.minimum_minor < 0:
            raise InvalidRequest("minimum_minor cannot be negative")

    def fee_for(self, amount_minor: int) -> int:
        if self.basis_points == 0 and self.minimum_minor == 0:
            return 0
        proportional = (amount_minor * self.basis_points + 9_999) // 10_000
        return max(proportional, self.minimum_minor)
