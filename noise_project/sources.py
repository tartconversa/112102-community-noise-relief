"""Load and validate the synthetic community-noise source tables.

The source tables are treated as untrusted input: every cross reference,
enumeration and numeric field is checked before the analysis engine sees it.
All identifiers, levels, amounts and places in ``sources/`` are synthetic.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import SourceError

EXPECTED_HEADERS: dict[str, list[str]] = {
    "monitor_locations": ["sensor", "area", "site_a", "site_b", "role"],
    "readings": ["sensor", "night", "observed_db", "background_db", "state", "weather", "road_work"],
    "operator_nights": ["operator", "first_night", "last_night", "nights_excluded"],
    "measurement_revisions": ["revision", "sensor", "night", "corrected_db", "learned_day", "reason"],
    "beneficiaries": ["building", "area", "resident_weight", "eligibility_cutoff_db"],
    "remedy_offers": ["remedy", "operator", "area", "building", "kind", "first_night", "last_night", "units", "cents"],
    "remedy_events": ["event", "remedy", "day", "state", "actual_building", "actual_units"],
    "budget": ["area", "funding_cents", "inspection_slots"],
}

READING_STATES = {"measured", "offline"}
WEATHER_STATES = {"dry", "wet"}
YES_NO = {"yes", "no"}
ROLES = {"exposed", "comparison"}
EVENT_STATES = {"proposal", "signed", "partial_acceptance"}
REMEDY_KINDS = {"night_reduction", "insulation"}
REVISION_REASONS = {"calibration", "drift", "repair", "other"}


def parse_nights(text: str) -> set[int]:
    """Parse ``8-11;14;16-18`` into an inclusive set of night numbers."""

    nights: set[int] = set()
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            try:
                first, last = int(start), int(end)
            except ValueError as exc:
                raise SourceError(f"invalid night range {part!r}") from exc
            if first > last:
                raise SourceError(f"night range {part!r} is reversed")
            nights.update(range(first, last + 1))
        else:
            try:
                nights.add(int(part))
            except ValueError as exc:
                raise SourceError(f"invalid night {part!r}") from exc
    return nights


def format_nights(nights: Iterable[int]) -> str:
    """Render a sorted night list with compact ranges."""

    values = sorted(set(nights))
    if not values:
        return "-"
    groups: list[list[int]] = [[values[0]]]
    for value in values[1:]:
        if value == groups[-1][-1] + 1:
            groups[-1].append(value)
        else:
            groups.append([value])
    chunks = []
    for group in groups:
        chunks.append(str(group[0]) if len(group) == 1 else f"{group[0]}-{group[-1]}")
    return ",".join(chunks)


def _int(value: str, field: str, row: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SourceError(f"row {row}: {field} must be an integer, got {value!r}") from exc


def _float(value: str, field: str, row: int) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SourceError(f"row {row}: {field} must be a number, got {value!r}") from exc


@dataclass(frozen=True)
class Sensor:
    sensor: str
    area: str
    sites: tuple[str, ...]
    role: str


@dataclass(frozen=True)
class Reading:
    sensor: str
    night: int
    observed_db: float | None
    background_db: float
    state: str
    weather: str
    road_work: bool


@dataclass(frozen=True)
class OperatorWindow:
    operator: str
    first_night: int
    last_night: int
    excluded: frozenset[int]
    nights: frozenset[int]


@dataclass(frozen=True)
class Revision:
    revision: str
    sensor: str
    night: int
    corrected_db: float
    learned_day: int
    reason: str


@dataclass(frozen=True)
class Building:
    building: str
    area: str
    resident_weight: int
    eligibility_cutoff_db: float


@dataclass(frozen=True)
class Offer:
    remedy: str
    operator: str
    area: str
    building: str | None
    kind: str
    first_night: int | None
    last_night: int | None
    units: float
    cents: int

    @property
    def window_nights(self) -> frozenset[int]:
        if self.first_night is None or self.last_night is None:
            return frozenset()
        return frozenset(range(self.first_night, self.last_night + 1))


@dataclass(frozen=True)
class RemedyEvent:
    event: str
    remedy: str
    day: int
    state: str
    actual_building: str | None
    actual_units: float | None


@dataclass(frozen=True)
class Budget:
    area: str
    funding_cents: int
    inspection_slots: int


@dataclass(frozen=True)
class Dataset:
    root: Path
    sensors: dict[str, Sensor]
    readings: dict[tuple[str, int], Reading]
    operators: dict[str, OperatorWindow]
    revisions: tuple[Revision, ...]
    buildings: dict[str, Building]
    offers: dict[str, Offer]
    events: tuple[RemedyEvent, ...]
    budgets: dict[str, Budget]
    source_hashes: dict[str, str]

    # -- convenience -------------------------------------------------
    @property
    def trial_nights(self) -> tuple[int, ...]:
        nights: set[int] = set()
        for window in self.operators.values():
            nights |= window.nights
        return tuple(sorted(nights))

    @property
    def observed_nights(self) -> tuple[int, ...]:
        nights = sorted({night for (_, night) in self.readings})
        return tuple(nights)

    @property
    def pretrial_nights(self) -> tuple[int, ...]:
        trial = self.trial_nights
        if not trial:
            return ()
        first = trial[0]
        return tuple(night for night in self.observed_nights if night < first)

    def sensors_in(self, area: str, role: str | None = None) -> list[Sensor]:
        selected = [s for s in self.sensors.values() if s.area == area]
        if role is not None:
            selected = [s for s in selected if s.role == role]
        return sorted(selected, key=lambda s: s.sensor)

    def buildings_in(self, area: str) -> list[Building]:
        return sorted(
            (b for b in self.buildings.values() if b.area == area),
            key=lambda b: b.building,
        )

    def operators_heard(self, area: str) -> list[str]:
        heard: set[str] = set()
        for sensor in self.sensors_in(area, "exposed"):
            heard.update(sensor.sites)
        return sorted(heard)

    def latest_revision_day(self) -> int:
        return max((r.learned_day for r in self.revisions), default=0)

    def revision_for(self, sensor: str, night: int, asof: int) -> Revision | None:
        applicable = [
            r
            for r in self.revisions
            if r.sensor == sensor and r.night == night and r.learned_day <= asof
        ]
        if not applicable:
            return None
        # Latest learned correction wins; revision id breaks ties deterministically.
        return max(applicable, key=lambda r: (r.learned_day, r.revision))


def _read_rows(path: Path, header: list[str]) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != header:
                raise SourceError(
                    f"{path.name}: expected header {header}, got {reader.fieldnames}"
                )
            rows = []
            for line_no, row in enumerate(reader, start=2):
                if None in row or any(value is None for value in row.values()):
                    raise SourceError(f"{path.name}: row {line_no} has the wrong field count")
                rows.append(row)
            if not rows:
                raise SourceError(f"{path.name}: no records")
            return rows
    except FileNotFoundError as exc:
        raise SourceError(f"missing source file: {path}") from exc


def load_dataset(root: str | Path) -> Dataset:
    """Load every source table below *root* and validate the cross references."""

    root = Path(root)
    rows = {name: _read_rows(root / f"{name}.csv", header) for name, header in EXPECTED_HEADERS.items()}
    hashes = {
        name: hashlib.sha256((root / f"{name}.csv").read_bytes()).hexdigest()
        for name in EXPECTED_HEADERS
    }

    sensors: dict[str, Sensor] = {}
    for line_no, row in enumerate(rows["monitor_locations"], start=2):
        sensor_id = row["sensor"]
        if sensor_id in sensors:
            raise SourceError(f"monitor_locations row {line_no}: duplicate sensor {sensor_id}")
        role = row["role"]
        if role not in ROLES:
            raise SourceError(f"monitor_locations row {line_no}: unknown role {role!r}")
        sites = tuple(value for value in (row["site_a"], row["site_b"]) if value)
        if role == "exposed" and not sites:
            raise SourceError(f"monitor_locations row {line_no}: exposed sensor without an operating site")
        if role == "comparison" and sites:
            raise SourceError(f"monitor_locations row {line_no}: comparison sensor must not name an operating site")
        sensors[sensor_id] = Sensor(sensor_id, row["area"], sites, role)

    readings: dict[tuple[str, int], Reading] = {}
    for line_no, row in enumerate(rows["readings"], start=2):
        sensor_id = row["sensor"]
        if sensor_id not in sensors:
            raise SourceError(f"readings row {line_no}: unknown sensor {sensor_id}")
        night = _int(row["night"], "night", line_no)
        if night <= 0:
            raise SourceError(f"readings row {line_no}: night must be positive")
        key = (sensor_id, night)
        if key in readings:
            raise SourceError(f"readings row {line_no}: duplicate reading {sensor_id}/{night}")
        state = row["state"]
        if state not in READING_STATES:
            raise SourceError(f"readings row {line_no}: unknown state {state!r}")
        weather = row["weather"]
        if weather not in WEATHER_STATES:
            raise SourceError(f"readings row {line_no}: unknown weather {weather!r}")
        road_work = row["road_work"] == "yes"
        if row["road_work"] not in YES_NO:
            raise SourceError(f"readings row {line_no}: road_work must be yes or no")
        background = _float(row["background_db"], "background_db", line_no)
        if not 20 <= background <= 140:
            raise SourceError(f"readings row {line_no}: background_db out of range")
        observed: float | None
        if state == "offline":
            if row["observed_db"]:
                raise SourceError(f"readings row {line_no}: offline reading must not carry observed_db")
            observed = None
        else:
            observed = _float(row["observed_db"], "observed_db", line_no)
            if not 20 <= observed <= 140:
                raise SourceError(f"readings row {line_no}: observed_db out of range")
        readings[key] = Reading(sensor_id, night, observed, background, state, weather, road_work)

    operators: dict[str, OperatorWindow] = {}
    for line_no, row in enumerate(rows["operator_nights"], start=2):
        operator = row["operator"]
        if operator in operators:
            raise SourceError(f"operator_nights row {line_no}: duplicate operator {operator}")
        first = _int(row["first_night"], "first_night", line_no)
        last = _int(row["last_night"], "last_night", line_no)
        if first > last:
            raise SourceError(f"operator_nights row {line_no}: first_night after last_night")
        excluded = frozenset(parse_nights(row["nights_excluded"]))
        scheduled = set(range(first, last + 1))
        outside = excluded - scheduled
        if outside:
            raise SourceError(
                f"operator_nights row {line_no}: excluded nights outside range: {sorted(outside)}"
            )
        operators[operator] = OperatorWindow(
            operator, first, last, excluded, frozenset(scheduled - set(excluded))
        )

    revisions: list[Revision] = []
    seen_revisions: set[str] = set()
    seen_revision_cells: set[tuple[str, int]] = set()
    for line_no, row in enumerate(rows["measurement_revisions"], start=2):
        revision_id = row["revision"]
        if revision_id in seen_revisions:
            raise SourceError(f"measurement_revisions row {line_no}: duplicate revision {revision_id}")
        seen_revisions.add(revision_id)
        sensor_id = row["sensor"]
        if sensor_id not in sensors:
            raise SourceError(f"measurement_revisions row {line_no}: unknown sensor {sensor_id}")
        night = _int(row["night"], "night", line_no)
        if (sensor_id, night) in seen_revision_cells:
            raise SourceError(f"measurement_revisions row {line_no}: duplicate correction for {sensor_id}/{night}")
        seen_revision_cells.add((sensor_id, night))
        reading = readings.get((sensor_id, night))
        if reading is None:
            raise SourceError(f"measurement_revisions row {line_no}: correction for a missing observation")
        if reading.state != "measured":
            raise SourceError(f"measurement_revisions row {line_no}: correction for an offline night")
        corrected = _float(row["corrected_db"], "corrected_db", line_no)
        if not 20 <= corrected <= 140:
            raise SourceError(f"measurement_revisions row {line_no}: corrected_db out of range")
        learned = _int(row["learned_day"], "learned_day", line_no)
        if learned <= 0:
            raise SourceError(f"measurement_revisions row {line_no}: learned_day must be positive")
        reason = row["reason"]
        if reason not in REVISION_REASONS:
            raise SourceError(f"measurement_revisions row {line_no}: unknown reason {reason!r}")
        revisions.append(Revision(revision_id, sensor_id, night, corrected, learned, reason))

    buildings: dict[str, Building] = {}
    for line_no, row in enumerate(rows["beneficiaries"], start=2):
        building_id = row["building"]
        if building_id in buildings:
            raise SourceError(f"beneficiaries row {line_no}: duplicate building {building_id}")
        weight = _int(row["resident_weight"], "resident_weight", line_no)
        if weight <= 0:
            raise SourceError(f"beneficiaries row {line_no}: resident_weight must be positive")
        cutoff = _float(row["eligibility_cutoff_db"], "eligibility_cutoff_db", line_no)
        buildings[building_id] = Building(building_id, row["area"], weight, cutoff)

    offers: dict[str, Offer] = {}
    for line_no, row in enumerate(rows["remedy_offers"], start=2):
        remedy = row["remedy"]
        if remedy in offers:
            raise SourceError(f"remedy_offers row {line_no}: duplicate remedy {remedy}")
        kind = row["kind"]
        if kind not in REMEDY_KINDS:
            raise SourceError(f"remedy_offers row {line_no}: unknown kind {kind!r}")
        building_id = row["building"] or None
        if building_id is not None:
            if building_id not in buildings:
                raise SourceError(f"remedy_offers row {line_no}: unknown building {building_id}")
            if buildings[building_id].area != row["area"]:
                raise SourceError(f"remedy_offers row {line_no}: building is not in the offer area")
        units = _float(row["units"], "units", line_no)
        if units <= 0:
            raise SourceError(f"remedy_offers row {line_no}: units must be positive")
        cents = _int(row["cents"], "cents", line_no)
        if cents <= 0:
            raise SourceError(f"remedy_offers row {line_no}: cents must be positive")
        first = int(row["first_night"]) if row["first_night"] else None
        last = int(row["last_night"]) if row["last_night"] else None
        if kind == "night_reduction":
            if first is None or last is None:
                raise SourceError(f"remedy_offers row {line_no}: night reduction needs a validity window")
            if first > last:
                raise SourceError(f"remedy_offers row {line_no}: remedy window is reversed")
            if units != int(units):
                raise SourceError(f"remedy_offers row {line_no}: night units must be whole nights")
        else:
            if building_id is None:
                raise SourceError(f"remedy_offers row {line_no}: insulation needs a target building")
            if first is not None or last is not None:
                raise SourceError(f"remedy_offers row {line_no}: insulation must not carry a night window")
            if units > 1:
                raise SourceError(f"remedy_offers row {line_no}: insulation units cannot exceed one building share")
        offers[remedy] = Offer(
            remedy, row["operator"], row["area"], building_id, kind, first, last, units, cents
        )

    events: list[RemedyEvent] = []
    seen_events: set[str] = set()
    for line_no, row in enumerate(rows["remedy_events"], start=2):
        event_id = row["event"]
        if event_id in seen_events:
            raise SourceError(f"remedy_events row {line_no}: duplicate event {event_id}")
        seen_events.add(event_id)
        remedy = row["remedy"]
        if remedy not in offers:
            raise SourceError(f"remedy_events row {line_no}: unknown remedy {remedy}")
        state = row["state"]
        if state not in EVENT_STATES:
            raise SourceError(f"remedy_events row {line_no}: unknown state {state!r}")
        actual_building = row["actual_building"] or None
        actual_units = (
            _float(row["actual_units"], "actual_units", line_no)
            if row["actual_units"]
            else None
        )
        if state == "partial_acceptance":
            if actual_building is None or actual_units is None:
                raise SourceError(f"remedy_events row {line_no}: partial acceptance needs building and units")
            offer = offers[remedy]
            if offer.kind != "insulation":
                raise SourceError(f"remedy_events row {line_no}: only insulation can be accepted")
            if actual_building not in buildings:
                raise SourceError(f"remedy_events row {line_no}: unknown actual building")
            if offer.building != actual_building:
                raise SourceError(
                    f"remedy_events row {line_no}: actual building does not match the offer"
                )
            if actual_units <= 0 or actual_units > offer.units:
                raise SourceError(f"remedy_events row {line_no}: actual_units outside the offer")
        else:
            if actual_building is not None or actual_units is not None:
                raise SourceError(f"remedy_events row {line_no}: {state} must not carry acceptance detail")
        day = _int(row["day"], "day", line_no)
        events.append(RemedyEvent(event_id, remedy, day, state, actual_building, actual_units))

    budgets: dict[str, Budget] = {}
    for line_no, row in enumerate(rows["budget"], start=2):
        area = row["area"]
        if area in budgets:
            raise SourceError(f"budget row {line_no}: duplicate area {area}")
        funding = _int(row["funding_cents"], "funding_cents", line_no)
        slots = _int(row["inspection_slots"], "inspection_slots", line_no)
        if funding < 0 or slots < 0:
            raise SourceError(f"budget row {line_no}: budget and slots must not be negative")
        budgets[area] = Budget(area, funding, slots)

    return Dataset(
        root=root,
        sensors=sensors,
        readings=readings,
        operators=operators,
        revisions=tuple(revisions),
        buildings=buildings,
        offers=offers,
        events=tuple(events),
        budgets=budgets,
        source_hashes=hashes,
    )
