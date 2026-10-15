"""Period rules (C4 §1). Pure functions; the pair rules are enforced on change runs from Phase 2
and on Phase 1 visual A/B comparisons."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from aether import science
from aether.domain.errors import AetherError


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def midpoint(self) -> date:
        return self.start + timedelta(days=(self.end - self.start).days // 2)

    def as_tuple(self) -> tuple[str, str]:
        return (self.start.isoformat(), self.end.isoformat())


def validate_period(p: Period) -> None:
    if p.end < p.start:
        raise AetherError("periods_invalid", "period end is before its start")
    if not (science.PERIOD_MIN_DAYS <= p.days <= science.PERIOD_MAX_DAYS):
        raise AetherError(
            "period_length_out_of_range",
            f"period length {p.days} days is outside {science.PERIOD_MIN_DAYS}–{science.PERIOD_MAX_DAYS}",
        )
    if p.start < date.fromisoformat(science.ARCHIVE_START):
        raise AetherError("period_before_archive", f"periods may not start before {science.ARCHIVE_START}")
    if p.end > date.today():
        raise AetherError("periods_invalid", "period ends in the future")


def doy_distance(a: date, b: date) -> int:
    """Circular day-of-year distance on a 365-day cycle (C4 §1)."""
    da = min(a.timetuple().tm_yday, 365)
    db = min(b.timetuple().tm_yday, 365)
    d = abs(da - db)
    return min(d, 365 - d)


def validate_pair(a: Period, b: Period) -> None:
    """All four C4 §1 rules for an A/B pair."""
    validate_period(a)
    validate_period(b)
    if not (a.start <= a.end < b.start <= b.end):
        raise AetherError("periods_invalid", "period A must end before period B starts")
    if doy_distance(a.midpoint, b.midpoint) > science.SEASON_MATCH_MAX_DOY:
        raise AetherError(
            "periods_not_season_matched",
            f"period midpoints are {doy_distance(a.midpoint, b.midpoint)} days apart in the year (max 30)",
        )
