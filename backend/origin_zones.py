"""Replaceable, dependency-free origin-zone lookup abstraction."""

from __future__ import annotations

from math import floor
from typing import Protocol

from backend.domain import Coordinates


class OriginZoneResolver(Protocol):
    def zone_for(self, coordinates: Coordinates) -> str: ...


class CoordinateGridOriginZoneResolver:
    """Coarse lookup accelerator only; Haversine still decides safe reuse."""

    def __init__(self, cell_size_degrees: float = 0.002) -> None:
        if cell_size_degrees <= 0:
            raise ValueError("cell_size_degrees must be positive")
        self.cell_size_degrees = cell_size_degrees

    def zone_for(self, coordinates: Coordinates) -> str:
        latitude_cell = floor(coordinates.latitude / self.cell_size_degrees)
        longitude_cell = floor(coordinates.longitude / self.cell_size_degrees)
        return f"grid:{self.cell_size_degrees:g}:{latitude_cell}:{longitude_cell}"
