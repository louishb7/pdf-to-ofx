"""Bounded, index-only accounting for hypothesis expansion and elimination."""

from dataclasses import dataclass, replace

from pdf_to_ofx.domain.evidence import MonetaryAssignment, SourceSpan
from pdf_to_ofx.domain.models import Chronology
from pdf_to_ofx.validation.controls import ControlInterval


@dataclass(frozen=True, slots=True)
class SearchStep:
    state: int
    parent: int | None
    chronology: Chronology
    choices: tuple[MonetaryAssignment, ...]
    date_source: SourceSpan | None
    complete: bool
    outcome: str = 'expanded'
    constraint: str | None = None
    controls: tuple[ControlInterval, ...] = ()


class SearchLedger:
    """Every visited partial/complete state consumes the same global budget."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.steps: list[SearchStep] = []
        self.completed = 0
        self.pruned: dict[str, int] = {}
        self.unsupported: set[str] = set()
        self.exhausted = False

    def open(self, parent: int | None, chronology: Chronology, choices: tuple[MonetaryAssignment, ...],
             date_source: SourceSpan | None, complete: bool) -> int | None:
        if self.exhausted or len(self.steps) >= self.limit:
            self.exhausted = True
            return None
        state = len(self.steps)
        self.steps.append(SearchStep(state, parent, chronology, choices, date_source, complete))
        self.completed += complete
        return state

    def reject(self, constraint: str, *, state: int | None = None, capability_missing: bool = False) -> None:
        self.pruned[constraint] = self.pruned.get(constraint, 0) + 1
        if capability_missing:
            self.unsupported.add(constraint)
        if state is not None:
            self.steps[state] = replace(self.steps[state], outcome='pruned', constraint=constraint)

    def survive(self, state: int, controls: tuple[ControlInterval, ...]) -> None:
        self.steps[state] = replace(self.steps[state], outcome='survived', controls=controls)
