"""Library-independent digital text and optional positioned words."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    page: int


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    number: int
    text: str
    words: tuple[Word, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    pages: tuple[ExtractedPage, ...]
