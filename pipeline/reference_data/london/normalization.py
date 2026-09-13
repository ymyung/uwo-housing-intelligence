"""Conservative, deterministic street-address normalization."""
from __future__ import annotations

from dataclasses import dataclass
import re

_PUNCTUATION = re.compile(r"[^A-Z0-9# /-]+")
_SPACE = re.compile(r"\s+")
_UNIT = re.compile(r"(?:#|UNIT|APT(?:\.|ARTMENT)?|SUITE|RM|ROOM)\s*([A-Z0-9-]+)$")
_SUFFIXES = {
    "STREET": "ST", "ST": "ST", "AVENUE": "AVE", "AVE": "AVE",
    "ROAD": "RD", "RD": "RD", "DRIVE": "DR", "DR": "DR",
    "COURT": "CT", "CT": "CT", "PLACE": "PL", "PL": "PL",
    "CRESCENT": "CRES", "CRES": "CRES", "BOULEVARD": "BLVD", "BLVD": "BLVD",
    "LANE": "LN", "LN": "LN", "TERRACE": "TER", "TER": "TER", "WAY": "WAY",
}
_DIRECTIONS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "N": "N", "S": "S", "E": "E", "W": "W"}


@dataclass(frozen=True)
class NormalizedAddress:
    address: str | None
    civic_address: str | None
    unit: str | None


def normalize_address(value: object) -> NormalizedAddress:
    if value is None or not str(value).strip():
        return NormalizedAddress(None, None, None)
    text = _SPACE.sub(" ", _PUNCTUATION.sub(" ", str(value).upper())).strip()
    match = _UNIT.search(text)
    unit = match.group(1) if match else None
    civic = text[:match.start()].strip() if match else text
    words = civic.split()
    if words and words[-1] in _DIRECTIONS:
        words[-1] = _DIRECTIONS[words[-1]]
    if words and words[-1] in _SUFFIXES:
        words[-1] = _SUFFIXES[words[-1]]
    elif len(words) > 1 and words[-2] in _SUFFIXES:
        words[-2] = _SUFFIXES[words[-2]]
    civic = " ".join(words)
    address = f"{civic} UNIT {unit}" if unit else civic
    return NormalizedAddress(address, civic or None, unit)


def normalize_official_address(attributes: dict[str, object]) -> NormalizedAddress:
    full = attributes.get("UnitFullAddress") or attributes.get("FullAddress")
    normalized = normalize_address(full)
    if normalized.civic_address:
        return normalized
    parts = " ".join(str(attributes.get(key) or "") for key in (
        "MunicipalNumber", "MunicipalNumberQualifier", "StreetName", "StreetType", "StreetDirection"
    ))
    base = normalize_address(parts)
    unit = normalize_address(f"UNIT {attributes.get('UnitNumber')}").unit if attributes.get("UnitNumber") else None
    return NormalizedAddress(f"{base.civic_address} UNIT {unit}" if base.civic_address and unit else base.civic_address, base.civic_address, unit)
