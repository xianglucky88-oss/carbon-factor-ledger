"""Unified unit registry and deterministic unit conversion.

All activity quantities are converted to the factor's *reference unit* before
multiplication.  Units are grouped by dimension; converting across dimensions
(e.g. kg -> kWh) raises :class:`UnitError` and the consuming record is then
quarantined with ``UNIT_INCOMPATIBLE`` instead of being treated as zero.

Emission units are handled separately (kgCO2e, tCO2e, gCO2e) so every reported
footprint is expressed in kgCO2e.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


class UnitError(ValueError):
    """Raised on unknown units or cross-dimension conversion."""


@dataclass(frozen=True)
class UnitDef:
    name: str
    dimension: str
    to_base: float                      # multiply value -> base unit of dimension


# Base units: mass=kg, energy=kWh, distance=km, mass*distance=tkm, volume=m3, area=m2
_UNITS: Dict[str, UnitDef] = {}


def _register(name: str, dimension: str, to_base: float) -> None:
    _UNITS[name] = UnitDef(name=name, dimension=dimension, to_base=to_base)


# Mass (base kg)
_register("kg", "mass", 1.0)
_register("g", "mass", 1e-3)
_register("t", "mass", 1000.0)          # metric tonne
_register("tonne", "mass", 1000.0)
_register("lb", "mass", 0.45359237)
# Energy (base kWh)
_register("kWh", "energy", 1.0)
_register("MWh", "energy", 1000.0)
_register("GJ", "energy", 277.7777777777778)
_register("MJ", "energy", 0.2777777777777778)
# Distance (base km)
_register("km", "distance", 1.0)
_register("m", "distance", 1e-3)
_register("mi", "distance", 1.609344)
# Freight mass-distance (base tkm = tonne-kilometre)
_register("tkm", "mass_distance", 1.0)
_register("tonne_km", "mass_distance", 1.0)
_register("kgkm", "mass_distance", 1e-3)
# Volume (base m3) and area (base m2)
_register("m3", "volume", 1.0)
_register("L", "volume", 1e-3)
_register("litre", "volume", 1e-3)
_register("m2", "area", 1.0)
_register("ha", "area", 10000.0)

# Emission units -> kgCO2e.
_EMISSION_TO_KG: Dict[str, float] = {
    "kgCO2e": 1.0,
    "gCO2e": 1e-3,
    "tCO2e": 1000.0,
}


def unit_def(name: str) -> UnitDef:
    try:
        return _UNITS[name]
    except KeyError as exc:
        raise UnitError(f"unknown unit: {name!r}") from exc


def dimension_of(name: str) -> str:
    return unit_def(name).dimension


def parse_factor_unit(text: str) -> Tuple[str, str]:
    """Split a factor unit string like ``kgCO2e/kg`` into its two parts."""
    if not isinstance(text, str) or "/" not in text:
        raise UnitError(f"factor unit must look like 'kgCO2e/kg', got {text!r}")
    numerator, denominator = text.split("/", 1)
    numerator = numerator.strip()
    denominator = denominator.strip()
    if numerator not in _EMISSION_TO_KG:
        raise UnitError(f"unknown emission unit: {numerator!r}")
    unit_def(denominator)  # validates the activity side
    return numerator, denominator


def convert_quantity(value: float, source_unit: str, target_unit: str) -> float:
    """Convert ``value`` from ``source_unit`` to ``target_unit``.

    Raises :class:`UnitError` if either unit is unknown or their dimensions
    differ.  Same-unit conversion is exact (no float drift).
    """
    src = unit_def(source_unit)
    dst = unit_def(target_unit)
    if src.dimension != dst.dimension:
        raise UnitError(
            f"unit incompatible: {source_unit!r} ({src.dimension}) cannot be "
            f"converted to {target_unit!r} ({dst.dimension})"
        )
    if source_unit == target_unit:
        return float(value)
    return float(value) * src.to_base / dst.to_base


def emission_to_kgco2e(value: float, emission_unit: str) -> float:
    try:
        return float(value) * _EMISSION_TO_KG[emission_unit]
    except KeyError as exc:
        raise UnitError(f"unknown emission unit: {emission_unit!r}") from exc
