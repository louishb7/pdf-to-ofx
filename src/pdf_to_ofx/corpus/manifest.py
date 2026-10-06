"""Versioned local metadata, never copied into diagnostic reports."""

from dataclasses import dataclass, fields
from enum import StrEnum
import re
from typing import Self


class SourceKind(StrEnum):
    OFFICIAL_STANDARD = "official_standard"
    OFFICIAL_BANK = "official_bank"
    OFFICIAL_REGULATOR = "official_regulator"
    PUBLIC_EXAMPLE = "public_example"
    COMMUNITY_EXAMPLE = "community_example"
    PRIVATE_ACCEPTANCE = "private_acceptance"
    SYNTHETIC = "synthetic"


@dataclass(frozen=True, slots=True)
class CorpusEntry:
    schema_version: int = 1
    corpus_id: str | None = None
    local_path: str | None = None
    sha256: str | None = None
    source_url: str | None = None
    source_kind: SourceKind | None = None
    institution: str | None = None
    country: str | None = None
    region: str | None = None
    document_kind: str | None = None
    language: str | None = None
    public_or_private: str | None = None
    retrieved_at: str | None = None
    notes: str | None = None

    @classmethod
    def from_dict(cls, value: object) -> Self:
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise ValueError("Invalid manifest schema.")
        version = value.get("schema_version", 1)
        if type(version) is not int or version != 1:
            raise ValueError("Unsupported manifest version.")
        for key, item in value.items():
            if key != "schema_version" and item is not None and not isinstance(item, str):
                raise ValueError("Manifest metadata must be text or null.")
            if isinstance(item, str):
                item.encode("utf-8")
        data = dict(value)
        if data.get("sha256") is not None:
            if not re.fullmatch(r"[0-9a-fA-F]{64}", data["sha256"]):
                raise ValueError("Invalid manifest hash.")
            data["sha256"] = data["sha256"].lower()
        if data.get("source_kind") is not None:
            data["source_kind"] = SourceKind(data["source_kind"])
        if data.get("public_or_private") not in (None, "public", "private"):
            raise ValueError("Invalid visibility classification.")
        return cls(**data)
