"""Library-independent extracted pages; positions can be added here later."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    number: int
    text: str


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    pages: tuple[ExtractedPage, ...]
