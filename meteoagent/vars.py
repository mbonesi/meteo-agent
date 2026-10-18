"""Vocabulary of canonical variables.

Every source converts into these names and these units. This is the single
place where the system decides "what wind_speed_10m means", and it's what
lets rules be written once and hold for both forecast and observation.

Note: VarSpec.label and describe() feed straight into the Telegram
notification text (message.py), which stays in Italian by design (see
README) — do not translate those literals.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VarSpec:
    name: str
    unit: str
    label: str
    decimals: int = 1

    def format(self, value: float | None) -> str:
        if value is None:
            return "n/d"
        return f"{value:.{self.decimals}f} {self.unit}".replace(".0 ", " ")


VARS: dict[str, VarSpec] = {
    v.name: v
    for v in (
        VarSpec("temperature_2m", "°C", "temperatura"),
        VarSpec("relative_humidity_2m", "%", "umidita' relativa", 0),
        VarSpec("precipitation", "mm", "precipitazione"),
        VarSpec("pressure_msl", "hPa", "pressione sul livello del mare"),
        VarSpec("wind_speed_10m", "km/h", "vento medio a 10 m"),
        VarSpec("wind_direction_10m", "°", "direzione vento a 10 m", 0),
        VarSpec("wind_gusts_10m", "km/h", "raffica massima a 10 m"),
        # ARPAE observation only (B11043): Open-Meteo does not forecast gust direction.
        VarSpec("wind_gusts_direction_10m", "°", "direzione della raffica", 0),
    )
}

# Derived variables computed over a rolling window from the series
# (derived_name, source_variable, hours, function).
DERIVED: tuple[tuple[str, str, int, str], ...] = (
    ("precipitation_sum_3h", "precipitation", 3, "sum"),
    ("precipitation_sum_6h", "precipitation", 6, "sum"),
    ("precipitation_sum_24h", "precipitation", 24, "sum"),
    ("wind_gusts_max_3h", "wind_gusts_10m", 3, "max"),
)

ALL_NAMES: frozenset[str] = frozenset(VARS) | frozenset(d[0] for d in DERIVED)


def describe(var: str, value: float | None) -> str:
    spec = VARS.get(var)
    if spec is None:
        return f"{var}={value}"
    return f"{spec.label}: {spec.format(value)}"
