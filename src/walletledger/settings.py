import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    pool_min_size: int = 1
    pool_max_size: int = 10
    auto_migrate: bool = True
    transfer_fee_basis_points: int = 0
    transfer_fee_minimum_minor: int = 0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        return cls(
            database_url=env.get(
                "LEDGER_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/walletledger"
            ),
            pool_min_size=int(env.get("LEDGER_POOL_MIN_SIZE", "1")),
            pool_max_size=int(env.get("LEDGER_POOL_MAX_SIZE", "10")),
            auto_migrate=env.get("LEDGER_AUTO_MIGRATE", "true").lower() in {"1", "true", "yes"},
            transfer_fee_basis_points=int(env.get("LEDGER_TRANSFER_FEE_BPS", "0")),
            transfer_fee_minimum_minor=int(env.get("LEDGER_TRANSFER_FEE_MINIMUM_MINOR", "0")),
        )
