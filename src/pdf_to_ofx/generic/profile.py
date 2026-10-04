"""A portable structural schema. No account, period, amount or source text."""

from dataclasses import asdict, dataclass
from enum import StrEnum
import json

from pdf_to_ofx.generic.structure import Tolerances


class DateMode(StrEnum):
    GROUPED = "grouped"
    PER_TRANSACTION = "per_transaction"


class AmountMode(StrEnum):
    SIGNED = "signed"
    CREDIT_DEBIT_MARKER = "credit_debit_marker"


class BalanceMode(StrEnum):
    RUNNING = "running_balance"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class LayoutProfile:
    date_mode: DateMode
    amount_mode: AmountMode
    balance_mode: BalanceMode
    movement_column: int = 0
    balance_column: int | None = 1
    carry_date_across_pages: bool = True
    footer_rows: int = 0
    tolerances: Tolerances = Tolerances()

    def __post_init__(self) -> None:
        if (not isinstance(self.date_mode, DateMode) or not isinstance(self.amount_mode, AmountMode)
                or not isinstance(self.balance_mode, BalanceMode) or not isinstance(self.tolerances, Tolerances)
                or type(self.carry_date_across_pages) is not bool
                or type(self.footer_rows) is not int or self.footer_rows < 0
                or type(self.movement_column) is not int):
            raise ValueError("Invalid structural layout profile.")
        if self.balance_mode == BalanceMode.RUNNING:
            if type(self.balance_column) is not int or {self.movement_column, self.balance_column} != {0, 1}:
                raise ValueError("Running balance requires two distinct monetary columns.")
        elif self.movement_column != 0 or self.balance_column is not None:
            raise ValueError("Absent balance requires a single movement column.")

    def to_json(self) -> str:
        return json.dumps({"version": 1, **asdict(self)}, ensure_ascii=True, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "LayoutProfile":
        try:
            fields = json.loads(text, object_pairs_hook=_unique_keys)
            required = {"date_mode", "amount_mode", "balance_mode"}
            allowed = required | {"version", "movement_column", "balance_column", "carry_date_across_pages", "footer_rows", "tolerances"}
            if (not isinstance(fields, dict) or not required <= fields.keys()
                    or not fields.keys() <= allowed or type(fields.get("version", 1)) is not int
                    or fields.get("version", 1) != 1):
                raise ValueError
            fields.pop("version", None)
            fields["date_mode"] = DateMode(fields["date_mode"])
            fields["amount_mode"] = AmountMode(fields["amount_mode"])
            fields["balance_mode"] = BalanceMode(fields["balance_mode"])
            if fields["balance_mode"] == BalanceMode.ABSENT:
                fields.setdefault("balance_column", None)
            fields["tolerances"] = Tolerances(**fields.get("tolerances", {}))
            return cls(**fields)
        except (TypeError, ValueError, KeyError):
            raise ValueError("Invalid structural layout JSON.") from None


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError("Duplicate profile field.")
        fields[key] = value
    return fields
