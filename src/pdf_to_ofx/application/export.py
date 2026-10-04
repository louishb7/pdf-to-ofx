"""Publish an explicitly requested OFX without replacing existing files."""

from pathlib import Path
from tempfile import NamedTemporaryFile


def write_ofx(output: Path, contents: str) -> None:
    # The temporary file is owner-only on POSIX. A hard link publishes the
    # complete file atomically and fails if the destination already exists.
    with NamedTemporaryFile(dir=output.parent, prefix=".pdf-to-ofx-", delete=False) as file:
        temporary = Path(file.name)
    try:
        temporary.write_text(contents, encoding="ascii", newline="\n")
        output.hardlink_to(temporary)
    finally:
        temporary.unlink(missing_ok=True)
