"""Committed City of London dataset configuration and snapshot metadata."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    source_url: str
    source_dataset_id: str
    geometry_type: str
    source_id_field: str
    geometry_family: str = "point"
    table_name: str | None = None
    category: str = "property_resolution"
    refresh_policy: str = "manual"
    enabled: bool = True
    source_last_edit_field: str | None = None
    field_mapping: dict[str, str] = field(default_factory=dict)
    required_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class LondonSettings:
    snapshot_root: Path
    validation_root: Path
    native_srid: int
    importer_version: str
    datasets: dict[str, DatasetSpec]
    source_organization: str = "Corporation of the City of London"
    bounds: tuple[float, float, float, float] = (460000, 4740000, 510000, 4780000)

    def enabled_datasets(self) -> list[DatasetSpec]:
        return [spec for spec in self.datasets.values() if spec.enabled]


def load_settings(path: Path | None = None) -> LondonSettings:
    config_path = path or PROJECT_ROOT / "config" / "london-reference-data.toml"
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    london = raw["london"]
    return LondonSettings(
        snapshot_root=PROJECT_ROOT / london["snapshot_root"],
        validation_root=PROJECT_ROOT / london["validation_root"],
        native_srid=int(london["native_srid"]),
        importer_version=london["importer_version"],
        source_organization=london.get("source_organization", "Corporation of the City of London"),
        bounds=(
            float(london.get("minimum_x", 460000)),
            float(london.get("minimum_y", 4740000)),
            float(london.get("maximum_x", 510000)),
            float(london.get("maximum_y", 4780000)),
        ),
        datasets={name: _dataset_spec(name, values) for name, values in raw["datasets"].items()},
    )


def _dataset_spec(name: str, values: dict[str, object]) -> DatasetSpec:
    raw = dict(values)
    field_mapping = {
        str(key): str(value)
        for key, value in dict(raw.pop("field_mapping", {})).items()
    }
    required_fields = tuple(str(value) for value in raw.pop("required_fields", []))
    return DatasetSpec(
        name=name,
        field_mapping=field_mapping,
        required_fields=required_fields,
        **raw,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
