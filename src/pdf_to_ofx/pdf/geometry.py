"""Relative geometry helpers; original PDF coordinates remain untouched."""

from math import hypot, isfinite

from pdf_to_ofx.pdf.document import ExtractedPage, Word


def _dimension(value: float | None) -> float:
    if type(value) not in (int, float) or not isfinite(value) or value <= 0:
        raise ValueError("A finite positive page dimension is required.")
    return value


def _tolerance(value: float) -> float:
    if type(value) not in (int, float) or not isfinite(value) or value < 0:
        raise ValueError("Geometry tolerance must be finite and nonnegative.")
    return value


def normalized_x(word: Word, page: ExtractedPage) -> float:
    if word.page != page.number:
        raise ValueError("Word and page differ.")
    return word.x0 / _dimension(page.width)


def normalized_y(word: Word, page: ExtractedPage) -> float:
    if word.page != page.number:
        raise ValueError("Word and page differ.")
    return word.top / _dimension(page.height)


def same_row(left: Word, right: Word, tolerance: float = 3.0) -> bool:
    distance = _tolerance(tolerance)
    return left.page == right.page and abs((left.top + left.bottom - right.top - right.bottom) / 2) <= distance


def aligned_with(left: Word, right: Word, tolerance: float = 3.0) -> bool:
    return abs(left.x0 - right.x0) <= _tolerance(tolerance)


def near(left: Word, right: Word, tolerance: float = 12.0) -> bool:
    distance = _tolerance(tolerance)
    return left.page == right.page and hypot(left.x0 - right.x0, left.top - right.top) <= distance
