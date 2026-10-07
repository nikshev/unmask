# impl: FR-005-02
"""Ground truth loader and validation for benchmark (005)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

__all__ = ["GroundTruthRecord", "load_ground_truth", "ValidationError"]

_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "specs" / "005-benchmark-funding-coordination" / "contracts" / "ground-truth.schema.json"

_SOURCE_ENUM = frozenset({
    "creator_wallet",
    "bundled_accounts",
    "court_filings",
    "exchange_listing",
    "manual_review",
})

_CLASS_ENUM = frozenset({"insider", "clean"})

_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class ValidationError(ValueError):
    """Ground truth validation error."""
    pass


@dataclass(frozen=True)
class GroundTruthRecord:
    version: int
    mint: str
    class_: str
    source: str
    notes: str = ""

    @property
    def class_label(self) -> str:
        return self.class_


def _load_schema() -> dict[str, Any]:
    with _SCHEMA_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


_SCHEMA = _load_schema()
_VALIDATOR = jsonschema.Draft202012Validator(_SCHEMA)


def _validate_mint(mint: str) -> None:
    if not _MINT_RE.match(mint):
        raise ValidationError(f"mint: invalid base58 address: {mint}")


def _validate_record(obj: dict[str, Any]) -> None:
    try:
        _VALIDATOR.validate(obj)
    except jsonschema.exceptions.ValidationError as exc:
        raise ValidationError(str(exc)) from exc
    _validate_mint(obj["mint"])
    if obj["class"] not in _CLASS_ENUM:
        raise ValidationError(f"class: must be one of {sorted(_CLASS_ENUM)}")
    if obj["source"] not in _SOURCE_ENUM:
        raise ValidationError(f"source: must be one of {sorted(_SOURCE_ENUM)}")
    if obj["version"] != 1:
        raise ValidationError("version: must be 1")


def load_ground_truth(path: Path | str) -> list[GroundTruthRecord]:
    """Load ground truth from JSONL file.

    Args:
        path: Path to JSONL file (one record per line).

    Returns:
        List of GroundTruthRecord in file order.

    Raises:
        ValidationError: If any record is invalid.
        FileNotFoundError: If file does not exist.
    """
    path = Path(path)
    records: list[GroundTruthRecord] = []
    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"line {line_num}: invalid JSON: {exc}") from exc
            _validate_record(obj)
            records.append(GroundTruthRecord(
                version=obj["version"],
                mint=obj["mint"],
                class_=obj["class"],
                source=obj["source"],
                notes=obj.get("notes", ""),
            ))
    return records


def load_ground_truth_dicts(path: Path | str) -> list[dict[str, Any]]:
    """Load ground truth as list of dicts (for internal use)."""
    return [r.__dict__ for r in load_ground_truth(path)]