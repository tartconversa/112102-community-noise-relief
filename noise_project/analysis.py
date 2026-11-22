"""Night-time evidence analysis for the low-altitude shuttle trial.

The engine deliberately keeps three questions apart, because the community
meeting needs them apart:

1. **Observed** — what the monitors actually recorded on which nights, and how
   complete that record is.  Measured nights are never inflated into "all
   nights"; wet nights in particular lose sensors to outages.
2. **Attributable** — the excess above the independent road-background source,
   compared with the pre-trial baseline and checked against comparison
   sub-areas.  Road works can invalidate only some comparison sub-areas, and a
   background shift makes a before/after comparison unsafe.
3. **Insufficient** — nights, buildings and areas where the record cannot
   support a claim, with the reason and the re-measurement that would fix it.

All levels are equivalent continuous levels in dB over one fixed night window.
Levels combine on the energy scale, so excess over background is
``10*log10(10**(obs/10) - 10**(bg/10))``.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Iterable

from .sources import (
    Building,
    Dataset,
    Revision,
    Sensor,
    format_nights,
)

# Night statuses for a building / night pair.
CORROBORATED = "corroborated"      # >= N sensors measured and all-at-or-above cutoff
CONFLICTING = "conflicting"        # >= 2 measured, at least one above, at least one below
SINGLE_SENSOR = "single_sensor"    # exactly one measured and above cutoff
BELOW = "below"                    # measured, none above cutoff
BELOW_PARTIAL = "below_partial"    # some sensors offline, the rest below cutoff
COVERAGE_GAP = "coverage_gap"      # no exposed sensor measured

POSITIVE_STATUSES = {CORROBORATED, SINGLE_SENSOR, CONFLICTING}
NEGATIVE_STATUSES = {BELOW, BELOW_PARTIAL}
GAP_STATUSES = {COVERAGE_GAP}


@dataclass(frozen=True)
class Params:
    """Analytic conventions chosen by the assessment team (and recorded)."""

    corroboration_min_sensors: int = 2
    eligibility_min_nights: int = 3
    background_tolerance_db: float = 1.0
    control_tolerance_db: float = 2.0
    population_apportioning: str = "equal_share_among_operators_heard"

    def as_dict(self) -> dict:
        return asdict(self)

    def digest(self) -> str:
        return _digest(self.as_dict())


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def db_energy(level_db: float) -> float:
    return 10.0 ** (level_db / 10.0)


def excess_over_background(observed_db: float, background_db: float) -> tuple[float, bool]:
    """Return (excess dB, at_or_below_background).

    The excess is the level of the residual source once the independently
    measured road background is removed on the energy scale.  If the combined
    level does not exceed the background, the residual is not distinguishable
    and the excess is reported as ``0.0`` with a flag.
    """

    residual = db_energy(observed_db) - db_energy(background_db)
    if residual <= 0:
        return 0.0, True
    return 10.0 * math.log10(residual), False


def _median(values: Iterable[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


class RevisionIndex:
    """All corrections (source + locally registered) usable as of a day."""

    def __init__(self, dataset: Dataset, overlay: Iterable[dict] = ()):
        self._base = list(dataset.revisions)
        self._overlay: list[Revision] = []
        for row in overlay:
            self._overlay.append(
                Revision(
                    revision=str(row["revision"]),
                    sensor=str(row["sensor"]),
                    night=int(row["night"]),
                    corrected_db=float(row["corrected_db"]),
                    learned_day=int(row["learned_day"]),
                    reason=str(row.get("reason", "other")),
                )
            )
        self._index: dict[tuple[str, int], list[Revision]] = {}
        for revision in [*self._base, *self._overlay]:
            self._index.setdefault((revision.sensor, revision.night), []).append(revision)

    def all(self) -> list[Revision]:
        return [*self._base, *self._overlay]

    def applicable(self, sensor: str, night: int, asof: int) -> Revision | None:
        candidates = [
            r for r in self._index.get((sensor, night), []) if r.learned_day <= asof
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: (r.learned_day, r.revision))

    def pending(self, sensor: str, night: int, asof: int) -> Revision | None:
        """A correction that exists but had not been learned by *asof*."""

        candidates = [
            r for r in self._index.get((sensor, night), []) if r.learned_day > asof
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda r: (r.learned_day, r.revision))


@dataclass
class NightValue:
    night: int
    observed_db: float | None
    background_db: float
    excess_db: float | None
    state: str
    weather: str
    road_work: bool
    revision: str | None
    pending_revision: str | None
    at_or_below_background: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SensorEvidence:
    sensor: str
    area: str
    role: str
    sites: tuple[str, ...]
    nights: list[NightValue]
    pretrial_excess_mean: float | None
    trial_excess_mean: float | None
    delta_db: float | None
    background_shift_db: float | None
    background_comparable: bool
    measured_trial_nights: int
    trial_nights: int

    def as_dict(self) -> dict:
        data = asdict(self)
        data["sites"] = list(self.sites)
        return data


@dataclass
class ControlAssessment:
    sensor: str
    area: str
    trial_excess_mean: float | None
    pretrial_excess_mean: float | None
    delta_db: float | None
    background_shift_db: float | None
    road_work_nights: list[int]
    valid_nights: list[int]
    clean: bool
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class AreaAttribution:
    area: str
    operators_heard: list[str]
    exposed_sensors: list[str]
    extra_db_mean: float | None
    extra_db_min: float | None
    extra_db_max: float | None
    per_operator_share_db: float | None
    status: str
    reasons: list[str]
    controls: list[ControlAssessment]
    comparable_group_residents: int
    per_operator_residents: dict[str, int]
    wet_trial_nights: list[int]
    dry_trial_nights: list[int]
    wet_offline_sensor_nights: int
    dry_offline_sensor_nights: int
    valid_nights: list[int]
    validity_note: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BuildingEvidence:
    building: str
    area: str
    cutoff_db: float
    resident_weight: int
    night_status: dict[int, str]
    confirmed_nights: list[int]
    single_sensor_nights: list[int]
    conflicting_nights: list[int]
    below_nights: list[int]
    coverage_gap_nights: list[int]
    pending_correction_nights: list[int]
    validated_nights: int
    plausible_upper_nights: int
    unknown_nights: int
    verified: bool
    verified_share: float
    operator_resident_share: dict[str, float]
    reasons: list[str]

    def as_dict(self) -> dict:
        data = asdict(self)
        data["night_status"] = {str(k): v for k, v in self.night_status.items()}
        return data


@dataclass
class AreaCoverage:
    area: str
    exposed_sensors: list[str]
    trial_nights: list[int]
    full_coverage_nights: list[int]
    partial_coverage_nights: list[int]
    unobserved_nights: list[int]
    wet_trial_nights: list[int]
    offline_sensor_nights: int
    wet_offline_sensor_nights: int
    trial_measured_fraction: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvidenceReport:
    asof_day: int
    params: Params
    trial_nights: list[int]
    pretrial_nights: list[int]
    observed_nights: list[int]
    coverage: list[AreaCoverage]
    sensors: list[SensorEvidence]
    attribution: list[AreaAttribution]
    buildings: list[BuildingEvidence]
    insufficient: list[dict]
    source_hashes: dict[str, str]
    revision_ids: list[str]
    digest: str = ""

    def __post_init__(self) -> None:
        if not self.digest:
            self.digest = _digest(self._payload())

    def _payload(self) -> dict:
        return {
            "asof_day": self.asof_day,
            "params": self.params.as_dict(),
            "trial_nights": self.trial_nights,
            "pretrial_nights": self.pretrial_nights,
            "observed_nights": self.observed_nights,
            "coverage": [c.as_dict() for c in self.coverage],
            "sensors": [s.as_dict() for s in self.sensors],
            "attribution": [a.as_dict() for a in self.attribution],
            "buildings": [b.as_dict() for b in self.buildings],
            "insufficient": self.insufficient,
            "source_hashes": self.source_hashes,
            "revision_ids": self.revision_ids,
        }

    def as_dict(self) -> dict:
        data = self._payload()
        data["digest"] = self.digest
        return data

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def building(self, building_id: str) -> BuildingEvidence:
        for row in self.buildings:
            if row.building == building_id:
                return row
        raise KeyError(building_id)

    @property
    def verified_buildings(self) -> list[BuildingEvidence]:
        return [b for b in self.buildings if b.verified]


class Analyzer:
    def __init__(self, dataset: Dataset, params: Params | None = None, overlay: Iterable[dict] = ()):
        self.dataset = dataset
        self.params = params or Params()
        self.revisions = RevisionIndex(dataset, overlay)
        self.overlay = list(overlay)

    # -- reading access ------------------------------------------------
    def corrected_value(self, sensor: str, night: int, asof: int) -> tuple[float | None, str | None, str | None]:
        reading = self.dataset.readings.get((sensor, night))
        if reading is None or reading.state != "measured" or reading.observed_db is None:
            return None, None, None
        revision = self.revisions.applicable(sensor, night, asof)
        pending = self.revisions.pending(sensor, night, asof)
        value = revision.corrected_db if revision else reading.observed_db
        return value, (revision.revision if revision else None), (pending.revision if pending else None)

    def _night_value(self, sensor: str, night: int, asof: int) -> NightValue:
        reading = self.dataset.readings[(sensor, night)]
        value, revision_id, pending_id = self.corrected_value(sensor, night, asof)
        if value is None:
            excess = None
            at_or_below = False
        else:
            excess, at_or_below = excess_over_background(value, reading.background_db)
        return NightValue(
            night=night,
            observed_db=value,
            background_db=reading.background_db,
            excess_db=excess,
            state=reading.state,
            weather=reading.weather,
            road_work=reading.road_work,
            revision=revision_id,
            pending_revision=pending_id,
            at_or_below_background=at_or_below,
        )

    # -- coverage ------------------------------------------------------
    def _coverage(self) -> list[AreaCoverage]:
        areas = sorted({s.area for s in self.dataset.sensors.values()})
        trial = list(self.dataset.trial_nights)
        result = []
        for area in areas:
            sensors = [s.sensor for s in self.dataset.sensors_in(area, "exposed")]
            full, partial, unobserved = [], [], []
            wet: list[int] = []
            offline = wet_offline = 0
            for night in trial:
                measured = 0
                night_is_wet = False
                for sensor in sensors:
                    reading = self.dataset.readings.get((sensor, night))
                    if reading is None:
                        continue
                    if reading.weather == "wet":
                        night_is_wet = True
                    if reading.state == "offline":
                        offline += 1
                        if reading.weather == "wet":
                            wet_offline += 1
                    else:
                        measured += 1
                if night_is_wet:
                    wet.append(night)
                if not sensors or measured == 0:
                    unobserved.append(night)
                elif measured == len(sensors):
                    full.append(night)
                else:
                    partial.append(night)
            total_sensor_nights = len(sensors) * len(trial)
            result.append(
                AreaCoverage(
                    area=area,
                    exposed_sensors=sensors,
                    trial_nights=trial,
                    full_coverage_nights=full,
                    partial_coverage_nights=partial,
                    unobserved_nights=unobserved,
                    wet_trial_nights=wet,
                    offline_sensor_nights=offline,
                    wet_offline_sensor_nights=wet_offline,
                    trial_measured_fraction=(
                        (total_sensor_nights - offline) / total_sensor_nights
                        if total_sensor_nights
                        else 0.0
                    ),
                )
            )
        return result

    # -- sensor series / attribution ----------------------------------
    @staticmethod
    def _residual_level(values: list[NightValue], night_set: set[int]) -> float | None:
        """Aggregate the residual source over a window on the energy scale.

        Averaging per-night excess dB is not an energy quantity and understates
        nights whose residual is small; convert the mean residual energy once.
        """

        energies = [
            db_energy(value.excess_db)
            for value in values
            if value.night in night_set and value.excess_db is not None
        ]
        if not energies:
            return None
        return 10.0 * math.log10(sum(energies) / len(energies))

    def _sensor_evidence(self, sensor: Sensor, asof: int) -> SensorEvidence:
        nights = sorted(
            night for (sensor_id, night) in self.dataset.readings if sensor_id == sensor.sensor
        )
        values = [self._night_value(sensor.sensor, night, asof) for night in nights]
        pretrial = set(self.dataset.pretrial_nights)
        trial = set(self.dataset.trial_nights)
        pre_mean = self._residual_level(values, pretrial)
        trial_mean = self._residual_level(values, trial)
        delta = trial_mean - pre_mean if pre_mean is not None and trial_mean is not None else None
        measured_trial = sum(1 for v in values if v.night in trial and v.excess_db is not None)
        pre_bg = [v.background_db for v in values if v.night in pretrial]
        trial_bg = [v.background_db for v in values if v.night in trial]
        pre_bg_med, trial_bg_med = _median(pre_bg), _median(trial_bg)
        shift = (
            trial_bg_med - pre_bg_med
            if pre_bg_med is not None and trial_bg_med is not None
            else None
        )
        comparable = shift is not None and abs(shift) <= self.params.background_tolerance_db
        return SensorEvidence(
            sensor=sensor.sensor,
            area=sensor.area,
            role=sensor.role,
            sites=sensor.sites,
            nights=values,
            pretrial_excess_mean=pre_mean,
            trial_excess_mean=trial_mean,
            delta_db=delta,
            background_shift_db=shift,
            background_comparable=comparable,
            measured_trial_nights=measured_trial,
            trial_nights=len(trial),
        )

    def _control(self, sensor: Sensor, asof: int) -> ControlAssessment:
        evidence = self._sensor_evidence(sensor, asof)
        trial = set(self.dataset.trial_nights)
        pre_bg = [
            v.background_db for v in evidence.nights if v.night in self.dataset.pretrial_nights
        ]
        pre_med = _median(pre_bg)
        road_work_nights: list[int] = []
        valid_nights: list[int] = []
        for value in evidence.nights:
            if value.night not in trial:
                continue
            if value.road_work:
                road_work_nights.append(value.night)
                continue
            if value.excess_db is None:
                continue
            shifted = (
                pre_med is not None
                and abs(value.background_db - pre_med) > self.params.background_tolerance_db
            )
            if shifted:
                continue
            valid_nights.append(value.night)
        # Aggregate the same way as the baseline: mean residual energy, then dB.
        valid_mean = self._residual_level(evidence.nights, set(valid_nights))
        delta = (
            valid_mean - evidence.pretrial_excess_mean
            if valid_mean is not None and evidence.pretrial_excess_mean is not None
            else None
        )
        reasons = []
        if road_work_nights:
            reasons.append(f"road work on nights {format_nights(road_work_nights)}")
        if evidence.background_shift_db is not None and not evidence.background_comparable:
            reasons.append(
                f"background shifted {evidence.background_shift_db:+.1f} dB, not comparable"
            )
        if not valid_nights:
            reasons.append("no comparable trial night")
        clean = (
            not reasons
            and delta is not None
            and abs(delta) <= self.params.control_tolerance_db
        )
        if delta is not None and abs(delta) > self.params.control_tolerance_db:
            reasons.append(f"control changed {delta:+.1f} dB, so it is not a clean control")
        if clean:
            reasons.append("stable comparison sub-area")
        return ControlAssessment(
            sensor=sensor.sensor,
            area=sensor.area,
            trial_excess_mean=valid_mean,
            pretrial_excess_mean=evidence.pretrial_excess_mean,
            delta_db=delta,
            background_shift_db=evidence.background_shift_db,
            road_work_nights=road_work_nights,
            valid_nights=valid_nights,
            clean=clean,
            reason="; ".join(reasons) if reasons else "no evidence",
        )

    def _attribution(self, asof: int) -> list[AreaAttribution]:
        all_controls = [
            self._control(s, asof)
            for s in self.dataset.sensors.values()
            if s.role == "comparison"
        ]
        result = []
        for area in sorted({s.area for s in self.dataset.sensors.values()}):
            exposed = self.dataset.sensors_in(area, "exposed")
            if not exposed:
                result.append(self._attribution_without_monitors(area))
                continue
            controls = [c for c in all_controls if c.area == area] or all_controls
            clean_controls = [c for c in controls if c.clean]
            evidences = [self._sensor_evidence(s, asof) for s in exposed]
            deltas = [e.delta_db for e in evidences if e.delta_db is not None]
            reasons: list[str] = []
            comparable = all(e.background_comparable for e in evidences)
            if not comparable:
                shifted = [e.sensor for e in evidences if not e.background_comparable]
                reasons.append(
                    "background is not comparable before and after the trial at "
                    + ", ".join(shifted)
                )
            if deltas:
                extra_mean = sum(deltas) / len(deltas)
                extra_min, extra_max = min(deltas), max(deltas)
            else:
                extra_mean = extra_min = extra_max = None
            # Equal-energy apportionment across the operators heard: a joint
            # level is not divided as if it were a linear quantity.
            heard_operators = self.dataset.operators_heard(area)
            if extra_mean is not None and heard_operators:
                shared_energy = db_energy(extra_mean) / len(heard_operators)
                per_operator_db = 10.0 * math.log10(shared_energy) if shared_energy > 0 else None
            else:
                per_operator_db = None
            if not deltas:
                reasons.append("no exposed monitor has both pre-trial and trial excess")
            elif extra_mean is not None and extra_mean <= 0:
                reasons.append("trial excess does not exceed the pre-trial baseline")
            if clean_controls:
                control_deltas = [c.delta_db for c in clean_controls if c.delta_db is not None]
                control_delta = sum(control_deltas) / len(control_deltas)
                if abs(control_delta) > self.params.control_tolerance_db:
                    reasons.append(f"clean comparison sub-areas moved {control_delta:+.1f} dB")
            else:
                reasons.append(
                    "no clean comparison sub-area: every comparison sensor is road-work "
                    "affected or background-shifted"
                )
            if not deltas:
                status = "insufficient"
            elif comparable and clean_controls:
                status = "validated"
            else:
                status = "confounded"
            operators = self.dataset.operators_heard(area)
            verified_residents = self._area_verified_residents(area, asof)
            share = verified_residents / len(operators) if operators else 0.0
            trial = list(self.dataset.trial_nights)
            wet_nights = [
                n
                for n in trial
                if any(
                    self.dataset.readings[(s.sensor, n)].weather == "wet"
                    for s in exposed
                    if (s.sensor, n) in self.dataset.readings
                )
            ]
            dry_nights = [n for n in trial if n not in wet_nights]
            wet_offline = dry_offline = 0
            valid_nights: set[int] = set()
            for s in exposed:
                for n in trial:
                    reading = self.dataset.readings.get((s.sensor, n))
                    if reading is None or reading.state != "offline":
                        continue
                    if reading.weather == "wet":
                        wet_offline += 1
                    else:
                        dry_offline += 1
            for evidence in evidences:
                pre_med = _median(
                    [v.background_db for v in evidence.nights if v.night in self.dataset.pretrial_nights]
                )
                for value in evidence.nights:
                    if value.night not in trial or value.excess_db is None:
                        continue
                    if pre_med is not None and abs(value.background_db - pre_med) > self.params.background_tolerance_db:
                        continue
                    valid_nights.add(value.night)
            valid_nights_sorted = sorted(valid_nights)
            if not valid_nights_sorted:
                validity_note = "no night carries a usable exposed-level comparison"
            else:
                excluded = sorted(set(trial) - valid_nights)
                partial_wet = sorted(
                    n
                    for n in wet_nights
                    if any(
                        self.dataset.readings[(s.sensor, n)].state == "offline"
                        for s in exposed
                        if (s.sensor, n) in self.dataset.readings
                    )
                )
                clauses = [f"comparable nights {format_nights(valid_nights_sorted)}"]
                if excluded:
                    clauses.append(f"excludes {format_nights(excluded)}")
                if partial_wet:
                    clauses.append(
                        f"wet nights {format_nights(partial_wet)} rest on partial coverage and "
                        "are reported separately, not extrapolated"
                    )
                validity_note = "conclusion applies to " + "; ".join(clauses)
            result.append(
                AreaAttribution(
                    area=area,
                    operators_heard=operators,
                    exposed_sensors=[s.sensor for s in exposed],
                    extra_db_mean=extra_mean,
                    extra_db_min=extra_min,
                    extra_db_max=extra_max,
                    per_operator_share_db=per_operator_db,
                    status=status,
                    reasons=reasons,
                    controls=controls,
                    comparable_group_residents=verified_residents,
                    per_operator_residents=(
                        {operator: int(round(share)) for operator in operators}
                        if operators
                        else {}
                    ),
                    wet_trial_nights=wet_nights,
                    dry_trial_nights=dry_nights,
                    wet_offline_sensor_nights=wet_offline,
                    dry_offline_sensor_nights=dry_offline,
                    valid_nights=valid_nights_sorted,
                    validity_note=validity_note,
                )
            )
        return result

    def _attribution_without_monitors(self, area: str) -> AreaAttribution:
        residents = sum(b.resident_weight for b in self.dataset.buildings_in(area))
        return AreaAttribution(
            area=area,
            operators_heard=self.dataset.operators_heard(area),
            exposed_sensors=[],
            extra_db_mean=None,
            extra_db_min=None,
            extra_db_max=None,
            per_operator_share_db=None,
            status="no_exposure_monitoring",
            reasons=[
                "the area has only comparison monitors, so no exposed measurement exists",
                f"{residents} residents in the area cannot be linked to an exposure measurement",
            ],
            controls=[],
            comparable_group_residents=0,
            per_operator_residents={},
            wet_trial_nights=[],
            dry_trial_nights=list(self.dataset.trial_nights),
            wet_offline_sensor_nights=0,
            dry_offline_sensor_nights=0,
            valid_nights=[],
            validity_note="no exposed measurement exists, so no validity window can be stated",
        )

    def _area_verified_residents(self, area: str, asof: int) -> int:
        total = 0
        for building in self.dataset.buildings_in(area):
            evidence = self._building_evidence(building, asof)
            if evidence.verified:
                total += building.resident_weight
        return total

    # -- building evidence --------------------------------------------
    def _building_evidence(self, building: Building, asof: int) -> BuildingEvidence:
        sensors = self.dataset.sensors_in(building.area, "exposed")
        trial = list(self.dataset.trial_nights)
        status: dict[int, str] = {}
        pending: list[int] = []
        for night in trial:
            measured: list[float] = []
            for sensor in sensors:
                reading = self.dataset.readings.get((sensor.sensor, night))
                value, _revision, pending_id = self.corrected_value(sensor.sensor, night, asof)
                if pending_id:
                    pending.append(night)
                if value is not None:
                    measured.append(value)
            positives = [value for value in measured if value >= building.eligibility_cutoff_db]
            if not sensors:
                status[night] = COVERAGE_GAP
            elif not measured:
                status[night] = COVERAGE_GAP
            elif len(measured) >= self.params.corroboration_min_sensors:
                if len(positives) >= self.params.corroboration_min_sensors:
                    status[night] = CORROBORATED
                elif positives:
                    status[night] = CONFLICTING
                else:
                    status[night] = BELOW
            else:  # exactly one measured
                if positives:
                    status[night] = SINGLE_SENSOR
                else:
                    status[night] = BELOW_PARTIAL
        pending = sorted(set(pending))
        confirmed = sorted(n for n, s in status.items() if s == CORROBORATED)
        single = sorted(n for n, s in status.items() if s == SINGLE_SENSOR)
        conflicting = sorted(n for n, s in status.items() if s == CONFLICTING)
        below = sorted(n for n, s in status.items() if s in {BELOW, BELOW_PARTIAL})
        gaps = sorted(n for n, s in status.items() if s == COVERAGE_GAP)
        verified = len(confirmed) >= self.params.eligibility_min_nights
        operators = self.dataset.operators_heard(building.area)
        share = building.resident_weight / len(operators) if operators else 0
        reasons = []
        if not sensors:
            reasons.append("no exposed monitor covers this building")
        if gaps:
            reasons.append(f"{len(gaps)} trial night(s) have no exposed measurement at all")
        if single:
            reasons.append(f"{len(single)} night(s) rest on a single sensor and cannot be corroborated")
        if conflicting:
            reasons.append(f"{len(conflicting)} night(s) have sensors that disagree")
        if pending:
            reasons.append(f"correction pending for night(s) {format_nights(pending)}")
        if len(confirmed) < self.params.eligibility_min_nights and sensors:
            reasons.append(
                f"only {len(confirmed)} corroborated night(s); the agreed boundary is "
                f"{self.params.eligibility_min_nights}"
            )
        return BuildingEvidence(
            building=building.building,
            area=building.area,
            cutoff_db=building.eligibility_cutoff_db,
            resident_weight=building.resident_weight,
            night_status=status,
            confirmed_nights=confirmed,
            single_sensor_nights=single,
            conflicting_nights=conflicting,
            below_nights=below,
            coverage_gap_nights=gaps,
            pending_correction_nights=pending,
            validated_nights=len(confirmed),
            plausible_upper_nights=len(confirmed) + len(single) + len(conflicting),
            unknown_nights=len(gaps),
            verified=verified,
            verified_share=1.0 if verified else 0.0,
            operator_resident_share={operator: share for operator in operators},
            reasons=reasons,
        )

    # -- assembled report ---------------------------------------------
    def report(self, asof: int | None = None) -> EvidenceReport:
        if asof is None:
            asof = max(
                self.dataset.latest_revision_day(),
                max((r.learned_day for r in self.revisions.all()), default=0),
            )
        coverage = self._coverage()
        sensors = [self._sensor_evidence(s, asof) for s in sorted(self.dataset.sensors.values(), key=lambda s: s.sensor)]
        attribution = self._attribution(asof)
        buildings = [
            self._building_evidence(b, asof)
            for b in sorted(self.dataset.buildings.values(), key=lambda b: b.building)
        ]
        insufficient = []
        for building in buildings:
            if not building.reasons:
                continue
            # Distinguish "this building fails the boundary" from "this building
            # passes, but some nights are still uncorroborated".  The latter is
            # a narrower limitation, not a missing claim.
            severity = "fails_boundary" if not building.verified else "partial_nights"
            insufficient.append(
                {
                    "scope": "building",
                    "id": building.building,
                    "area": building.area,
                    "severity": severity,
                    "verified": building.verified,
                    "reasons": list(building.reasons),
                    "re_measure_nights": sorted(
                        set(building.coverage_gap_nights)
                        | set(building.single_sensor_nights)
                        | set(building.conflicting_nights)
                    ),
                }
            )
        for area_cov in coverage:
            if area_cov.unobserved_nights:
                insufficient.append(
                    {
                        "scope": "area",
                        "id": area_cov.area,
                        "area": area_cov.area,
                        "reasons": [
                            "no exposed measurement on night(s) "
                            f"{format_nights(area_cov.unobserved_nights)}"
                        ],
                        "re_measure_nights": area_cov.unobserved_nights,
                    }
                )
        total_offline = sum(c.offline_sensor_nights for c in coverage)
        wet_offline = sum(c.wet_offline_sensor_nights for c in coverage)
        if total_offline and wet_offline == total_offline:
            wet_nights = sorted({n for c in coverage for n in c.wet_trial_nights})
            by_weather = {
                "wet": wet_offline,
                "dry": total_offline - wet_offline,
            }
            insufficient.append(
                {
                    "scope": "design",
                    "id": "wet_night_outage",
                    "area": "*",
                    "reasons": [
                        "every sensor outage in the record falls on a wet night "
                        f"({by_weather['wet']} of {total_offline} offline sensor-nights), so the "
                        "measured sample systematically under-represents rain nights and must "
                        "not be read as covering all nights"
                    ],
                    "re_measure_nights": wet_nights,
                }
            )
        return EvidenceReport(
            asof_day=asof,
            params=self.params,
            trial_nights=list(self.dataset.trial_nights),
            pretrial_nights=list(self.dataset.pretrial_nights),
            observed_nights=list(self.dataset.observed_nights),
            coverage=coverage,
            sensors=sensors,
            attribution=attribution,
            buildings=buildings,
            insufficient=insufficient,
            source_hashes=dict(self.dataset.source_hashes),
            revision_ids=sorted({r.revision for r in self.revisions.all()}),
        )


# ---------------------------------------------------------------------------
# Remediation planning
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    remedy: str
    operator: str
    area: str
    kind: str
    building: str | None
    cents: int
    units: float
    first_night: int | None
    last_night: int | None
    committed: bool
    verified_building: bool
    value: float
    status: str
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)




@dataclass
class FundedLine:
    remedy: str
    operator: str
    area: str
    kind: str
    building: str | None
    cents: int
    units: float
    nights: list[int]
    committed: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class AcceptanceLine:
    remedy: str
    operator: str
    building: str | None
    units: float
    nights: list[int]
    day: int
    source: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class CommitmentLine:
    remedy: str
    operator: str
    area: str
    kind: str
    building: str | None
    cents: int
    units: float
    state: str
    day: int
    reservation_released: bool
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeliveryLine:
    """What an acceptance actually delivered versus what it promised."""

    remedy: str
    operator: str
    kind: str
    building: str | None
    promised_units: float
    accepted_units: float
    outstanding_units: float
    accepted_days: list[int]
    fully_delivered: bool
    scope_note: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class AreaPlan:
    area: str
    funding_cents: int
    inspection_slots: int
    reserved_cents: int
    allocated_cents: int
    available_cents: int
    slots_used: int
    slots_remaining: int
    committed: list[CommitmentLine]
    funded: list[FundedLine]
    acceptances: list[AcceptanceLine]
    deliveries: list[DeliveryLine]
    outstanding_cents: int
    outstanding_units: float
    suspended: list[Candidate]
    deferred: list[Candidate]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlanReport:
    asof_day: int
    params: Params
    areas: list[AreaPlan]
    candidates: list[Candidate]
    commitments: list[CommitmentLine]
    pending_re_measure: list[dict]
    withheld_by_boundary: list[dict]
    notes: list[str]
    digest: str = ""

    def __post_init__(self) -> None:
        if not self.digest:
            self.digest = _digest(self._payload())

    def _payload(self) -> dict:
        return {
            "asof_day": self.asof_day,
            "params": self.params.as_dict(),
            "areas": [a.as_dict() for a in self.areas],
            "candidates": [c.as_dict() for c in self.candidates],
            "commitments": [c.as_dict() for c in self.commitments],
            "pending_re_measure": self.pending_re_measure,
            "withheld_by_boundary": self.withheld_by_boundary,
            "notes": self.notes,
        }

    def as_dict(self) -> dict:
        data = self._payload()
        data["digest"] = self.digest
        return data

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def allocate_nights(window: Iterable[int], units: int, used: set[int]) -> list[int] | None:
    """Pick ``units`` distinct nights inside *window* that are not already claimed."""

    available = sorted(set(window) - used)
    if len(available) < units:
        return None
    return available[:units]


class Planner:
    """Budget-aware remediation planner.

    Rules encoded here:

    * a signed commitment stays reserved until a cancellation is *confirmed*;
      a cancel request alone does not free money;
    * one building cannot carry two operators' insulation acceptances;
    * the same reduction night cannot discharge two operators' quotas, even
      across areas and even when they share a window;
    * an insulation remedy needs an inspection slot for its target building;
    * new insulation and night reductions are only funded for exposure that is
      validated at this as-of day; the rest is withheld for re-measurement;
    * population is apportioned across the operators heard in an area, so a
      multi-operator area never counts a full population for every party;
    * a signed commitment is still honoured if a later correction moves the
      eligibility boundary out from under it; only *new* proposals pause.
    """

    def __init__(
        self,
        dataset: Dataset,
        report: EvidenceReport,
        commitments: dict[str, dict] | None = None,
        acceptances: list[dict] | None = None,
        budgets: dict[str, int] | None = None,
        baseline_report: EvidenceReport | None = None,
    ):
        self.dataset = dataset
        self.report = report
        self.params = report.params
        self.commitments = commitments or {}
        self.acceptances = acceptances or []
        self.budgets = budgets or {
            area: budget.funding_cents for area, budget in dataset.budgets.items()
        }
        self.baseline_report = baseline_report

    # -- candidate scoring --------------------------------------------
    def _operator_population(self, area: str, operator: str) -> float:
        operators = self.dataset.operators_heard(area)
        if not operators or operator not in operators:
            return 0.0
        residents = sum(b.resident_weight for b in self.report.verified_buildings if b.area == area)
        return residents / len(operators)

    def _candidate(self, offer, commitment: dict | None) -> Candidate:
        building = self.dataset.buildings.get(offer.building) if offer.building else None
        evidence = self.report.building(building.building) if building else None
        verified = bool(evidence and evidence.verified)
        state = commitment.get("state") if commitment else None
        committed = state in {"signed", "cancel_requested"}
        cancelled = state == "cancelled"
        status = "candidate"
        reason = ""
        if offer.kind == "night_reduction":
            value = self._operator_population(offer.area, offer.operator) * offer.units
            if offer.operator not in self.dataset.operators_heard(offer.area):
                status, reason = "suspended", "operator is not heard in the target area"
            elif not any(b.area == offer.area and b.verified for b in self.report.buildings):
                status, reason = "suspended", "the area has no verified exposed building"
        else:
            value = float(building.resident_weight) if building else 0.0
            if building is None:
                status, reason = "suspended", "insulation offer has no target building"
            elif not verified and not committed:
                status, reason = "suspended", "target building is not verified at this as-of day"
        if committed:
            status = "committed"
            if state == "cancel_requested":
                reason = (
                    "cancellation requested but not confirmed; funding stays reserved"
                )
            else:
                reason = "signed commitment is reserved until a cancellation is confirmed"
        elif cancelled:
            status = "cancelled"
            reason = "confirmed cancellation released the reservation"
        return Candidate(
            remedy=offer.remedy,
            operator=offer.operator,
            area=offer.area,
            kind=offer.kind,
            building=offer.building,
            cents=offer.cents,
            units=offer.units,
            first_night=offer.first_night,
            last_night=offer.last_night,
            committed=committed,
            verified_building=verified,
            value=value,
            status=status,
            reason=reason,
        )

    # -- main ----------------------------------------------------------
    def plan(self) -> PlanReport:
        candidates = [
            self._candidate(offer, self.commitments.get(offer.remedy))
            for offer in sorted(self.dataset.offers.values(), key=lambda o: o.remedy)
        ]
        commitment_lines = [
            line
            for area in sorted(self.budgets)
            for line in self._area_commitments(area)
        ]
        acceptance_lines = self._acceptance_lines()

        by_area: dict[str, list[Candidate]] = {}
        for candidate in candidates:
            by_area.setdefault(candidate.area, []).append(candidate)
        for area in self.budgets:
            by_area.setdefault(area, [])

        areas: list[AreaPlan] = []
        used_nights: set[int] = set()
        for area in sorted(self.budgets):
            plan = self._solve_area(area, by_area.get(area, []), acceptance_lines, used_nights)
            used_nights |= {night for line in plan.funded for night in line.nights}
            used_nights |= {night for line in plan.acceptances for night in line.nights}
            areas.append(plan)

        pending = self._pending_re_measure()
        withheld = self._withheld_by_boundary(candidates)
        notes = []
        for candidate in candidates:
            if candidate.area not in self.budgets:
                notes.append(
                    f"{candidate.area} {candidate.remedy}: no budget line for this area, so "
                    "the offer is deferred rather than silently dropped"
                )
        for area in areas:
            for item in area.suspended:
                notes.append(f"{area.area} {item.remedy}: suspended — {item.reason}")
            for item in area.deferred:
                notes.append(f"{area.area} {item.remedy}: deferred — {item.reason}")
            if area.available_cents < 0:
                notes.append(
                    f"{area.area}: signed commitments exceed the funding line by "
                    f"{-area.available_cents} cents; they remain reserved"
                )
        for line in commitment_lines:
            if line.state == "cancel_requested":
                notes.append(
                    f"{line.remedy}: cancellation requested but not confirmed; its "
                    f"{line.cents} cents stay reserved and cannot be re-spent"
                )
        notes.append(
            "population is apportioned equally across the operators heard in each area; "
            "it is never counted in full for every operator, and complaint counts are not "
            "used as an exposure substitute"
        )
        return PlanReport(
            asof_day=self.report.asof_day,
            params=self.params,
            areas=areas,
            candidates=candidates,
            commitments=commitment_lines,
            pending_re_measure=pending,
            withheld_by_boundary=withheld,
            notes=notes,
        )

    def _acceptance_lines(self) -> list[AcceptanceLine]:
        lines = []
        for row in self.acceptances:
            offer = self.dataset.offers.get(row["remedy"])
            lines.append(
                AcceptanceLine(
                    remedy=row["remedy"],
                    operator=offer.operator if offer else "?",
                    building=row.get("building"),
                    units=float(row.get("units", 0)),
                    nights=sorted(int(n) for n in row.get("nights") or []),
                    day=int(row.get("day", 0)),
                    source=str(row.get("source_event") or "ledger"),
                )
            )
        return lines

    def _solve_area(
        self,
        area: str,
        pool: list[Candidate],
        acceptances: list[AcceptanceLine],
        used_globally: set[int],
    ) -> AreaPlan:
        budget = self.budgets.get(area, 0)
        slots = (
            self.dataset.budgets[area].inspection_slots
            if area in self.dataset.budgets
            else 0
        )
        area_acceptances = [a for a in acceptances if self._acceptance_in_area(a, area)]
        committed = [c for c in pool if c.status == "committed"]
        suspended = [c for c in pool if c.status in {"suspended", "cancelled"}]
        deferred: list[Candidate] = []
        optional = [
            c
            for c in pool
            if c.status == "candidate"
            and c.remedy not in {line.remedy for line in area_acceptances}
        ]

        funded: list[FundedLine] = []
        used_nights: set[int] = set()
        reserved = 0

        # Nights already claimed by recorded acceptances are unavailable to
        # every later proposal, in this area and any other.
        for line in area_acceptances:
            used_nights |= set(line.nights)

        # An inspection slot is reserved once per affected building, whether it
        # comes from a signed commitment or an already recorded acceptance.
        committed_slot_buildings: set[str] = {
            line.building for line in area_acceptances if line.building
        }

        # Signed commitments are reserved before anything else.
        committed_buildings = {c.building for c in committed if c.building}
        for candidate in committed:
            nights: list[int] = []
            if candidate.kind == "night_reduction":
                nights = allocate_nights(
                    range(candidate.first_night, candidate.last_night + 1),
                    int(candidate.units),
                    used_nights | used_globally,
                ) or []
                used_nights |= set(nights)
            elif candidate.building:
                committed_slot_buildings.add(candidate.building)
            reserved += candidate.cents
            funded.append(
                FundedLine(
                    remedy=candidate.remedy,
                    operator=candidate.operator,
                    area=candidate.area,
                    kind=candidate.kind,
                    building=candidate.building,
                    cents=candidate.cents,
                    units=candidate.units,
                    nights=nights,
                    committed=True,
                )
            )

        # Optional proposals must respect commitments, existing acceptances and
        # the effective remaining budget and slots.
        blocked = committed_buildings | committed_slot_buildings
        conflicted = [
            c for c in optional if c.building and c.building in blocked
        ]
        for candidate in conflicted:
            candidate.status = "deferred"
            candidate.reason = (
                f"building {candidate.building} already carries a signed commitment or a "
                "recorded acceptance, and one building cannot be claimed twice"
            )
            deferred.append(candidate)
        optional = [c for c in optional if c not in conflicted]
        budget_left = budget - reserved
        slots_left = max(slots - len(committed_slot_buildings), 0)
        best = self._search(optional, budget_left, slots_left, used_nights, used_globally)

        if best is not None:
            _key, chosen, nights = best
            for candidate in chosen:
                funded.append(
                    FundedLine(
                        remedy=candidate.remedy,
                        operator=candidate.operator,
                        area=candidate.area,
                        kind=candidate.kind,
                        building=candidate.building,
                        cents=candidate.cents,
                        units=candidate.units,
                        nights=nights.get(candidate.remedy, []),
                        committed=False,
                    )
                )
                used_nights |= set(nights.get(candidate.remedy, []))
                if candidate.kind == "insulation" and candidate.building:
                    committed_slot_buildings.add(candidate.building)
            chosen_ids = {c.remedy for c in chosen}
            for candidate in optional:
                if candidate.remedy not in chosen_ids:
                    candidate.status = "deferred"
                    candidate.reason = self._defer_reason(
                        candidate, budget_left, slots_left, used_nights, used_globally
                    )
                    deferred.append(candidate)
        else:
            for candidate in optional:
                candidate.status = "deferred" if budget_left > 0 else "suspended"
                candidate.reason = (
                    "no feasible allocation within the remaining budget and slots"
                    if budget_left > 0
                    else "no funding remains after reserved commitments"
                )
                if candidate.status == "deferred":
                    deferred.append(candidate)
                else:
                    suspended.append(candidate)

        allocated = sum(line.cents for line in funded if not line.committed)
        deliveries, outstanding_units = self._deliveries(area, funded, area_acceptances)
        cents_by_remedy = {line.remedy: line.cents for line in funded}
        outstanding_cents = 0
        for delivery in deliveries:
            if delivery.promised_units <= 0:
                continue
            share = delivery.outstanding_units / delivery.promised_units
            outstanding_cents += int(round(cents_by_remedy.get(delivery.remedy, 0) * share))
        return AreaPlan(
            area=area,
            funding_cents=budget,
            inspection_slots=slots,
            reserved_cents=reserved,
            allocated_cents=allocated,
            available_cents=budget - reserved - allocated,
            slots_used=len(committed_slot_buildings),
            slots_remaining=max(slots - len(committed_slot_buildings), 0),
            committed=self._area_commitments(area),
            funded=funded,
            acceptances=area_acceptances,
            deliveries=deliveries,
            outstanding_cents=outstanding_cents,
            outstanding_units=outstanding_units,
            suspended=suspended,
            deferred=deferred,
        )

    def _deliveries(
        self,
        area: str,
        funded: list[FundedLine],
        acceptances: list[AcceptanceLine],
    ) -> tuple[list[DeliveryLine], float]:
        """Match acceptances to promises, per remedy and per building.

        A single receipt only credits the units and building it names.  It never
        marks a whole area, or another operator's share of the same building,
        as delivered.
        """

        by_remedy: dict[str, list[AcceptanceLine]] = {}
        for line in acceptances:
            by_remedy.setdefault(line.remedy, []).append(line)
        promised: dict[str, tuple[str, str, str | None, float]] = {}
        for line in funded:
            promised[line.remedy] = (line.operator, line.kind, line.building, line.units)
        for candidate in self.commitments.values():
            remedy = candidate.get("remedy")
            offer = self.dataset.offers.get(remedy)
            if candidate.get("state") == "cancelled":
                # A confirmed cancellation ends the obligation; remaining units
                # are no longer counted as outstanding.
                continue
            if offer and remedy not in promised and offer.area == area:
                promised[remedy] = (
                    offer.operator,
                    offer.kind,
                    offer.building,
                    float(candidate.get("units", offer.units)),
                )
        # A recorded acceptance is itself a promise, even if this as-of day no
        # longer funds the offer; otherwise delivered work would disappear from
        # the outstanding calculation.
        for line in acceptances:
            if line.remedy in promised:
                continue
            offer = self.dataset.offers.get(line.remedy)
            if offer and offer.area == area:
                promised[line.remedy] = (
                    offer.operator,
                    offer.kind,
                    offer.building or line.building,
                    float(offer.units),
                )
        deliveries: list[DeliveryLine] = []
        outstanding_units = 0.0
        cancelled = {
            remedy
            for remedy, row in self.commitments.items()
            if row.get("state") == "cancelled"
        }
        for remedy, (operator, kind, building, units) in promised.items():
            lines = by_remedy.get(remedy, [])
            accepted = sum(line.units for line in lines)
            remaining = round(max(units - accepted, 0.0), 6)
            # A confirmed cancellation ends the undelivered obligation, but a
            # partial acceptance that already happened still counts as work.
            is_cancelled = remedy in cancelled
            if remaining and not is_cancelled:
                outstanding_units += remaining
            scope = "target building only" if kind == "insulation" else "per-night units"
            note = ""
            if is_cancelled:
                note = (
                    "commitment cancelled and confirmed; the undelivered remainder is no "
                    "longer outstanding, but accepted units stay on record"
                )
            elif kind == "insulation" and lines:
                note = (
                    f"receipt credits {building} only; other buildings in {area} are not "
                    "covered by this acceptance"
                )
            elif not lines:
                note = "no acceptance recorded yet"
            deliveries.append(
                DeliveryLine(
                    remedy=remedy,
                    operator=operator,
                    kind=kind,
                    building=building,
                    promised_units=units,
                    accepted_units=accepted,
                    outstanding_units=0.0 if is_cancelled else remaining,
                    accepted_days=sorted(line.day for line in lines),
                    fully_delivered=remaining == 0 and bool(lines),
                    scope_note=note or scope,
                )
            )
        return deliveries, outstanding_units

    def _acceptance_in_area(self, line: AcceptanceLine, area: str) -> bool:
        if line.building and line.building in self.dataset.buildings:
            return self.dataset.buildings[line.building].area == area
        offer = self.dataset.offers.get(line.remedy)
        return bool(offer and offer.area == area)

    def _area_commitments(self, area: str) -> list[CommitmentLine]:
        rows = []
        for offer in sorted(self.dataset.offers.values(), key=lambda o: o.remedy):
            if offer.area != area:
                continue
            commitment = self.commitments.get(offer.remedy)
            if not commitment:
                continue
            rows.append(
                CommitmentLine(
                    remedy=offer.remedy,
                    operator=offer.operator,
                    area=offer.area,
                    kind=offer.kind,
                    building=offer.building,
                    cents=int(commitment.get("cents", offer.cents)),
                    units=float(commitment.get("units", offer.units)),
                    state=str(commitment.get("state", "signed")),
                    day=int(commitment.get("day", 0)),
                    reservation_released=commitment.get("state") == "cancelled"
                    and bool(commitment.get("cancelled_confirmed")),
                    note=str(commitment.get("note", "")),
                )
            )
        return rows

    def _search(
        self,
        optional: list[Candidate],
        budget_left: int,
        slots_left: int,
        used_nights: set[int],
        used_globally: set[int],
    ):
        best = None
        for mask in range(1 << len(optional)):
            chosen = [optional[i] for i in range(len(optional)) if mask & (1 << i)]
            if sum(c.cents for c in chosen) > budget_left:
                continue
            if sum(1 for c in chosen if c.kind == "insulation") > slots_left:
                continue
            buildings = [c.building for c in chosen if c.building]
            if len(buildings) != len(set(buildings)):
                continue
            nights: dict[str, list[int]] = {}
            trial_used = set(used_nights) | set(used_globally)
            feasible = True
            for candidate in chosen:
                if candidate.kind != "night_reduction":
                    continue
                allocation = allocate_nights(
                    range(candidate.first_night, candidate.last_night + 1),
                    int(candidate.units),
                    trial_used,
                )
                if allocation is None:
                    feasible = False
                    break
                nights[candidate.remedy] = allocation
                trial_used |= set(allocation)
            if not feasible:
                continue
            # Prefer the highest verified-exposure value, then the cheapest and
            # widest combination, so the choice is reproducible.
            value = sum(c.value for c in chosen)
            key = (round(value, 6), -sum(c.cents for c in chosen), len(chosen))
            if best is None or key > best[0]:
                best = (key, chosen, nights)
        return best

    def _defer_reason(
        self,
        candidate: Candidate,
        budget_left: int,
        slots_left: int,
        used: set[int],
        used_globally: set[int],
    ) -> str:
        if candidate.cents > budget_left:
            return "insufficient remaining funding after reserved commitments"
        if candidate.kind == "insulation":
            if slots_left <= 0:
                return "no inspection slot left in this area"
            if candidate.building in {a.building for a in self.report.buildings if not a.verified}:
                return "target building is not verified at this as-of day"
            return "an alternative combination scored higher on verified exposure"
        window = set(range(candidate.first_night, candidate.last_night + 1))
        blocked = window & (used | used_globally)
        if len(window - blocked) < int(candidate.units):
            return (
                f"only {len(window - blocked)} distinct night(s) remain in the offer window; "
                f"{int(candidate.units)} are required and the same night cannot discharge "
                "two operators' quotas"
            )
        return "an alternative combination scored higher on verified exposure"

    def _pending_re_measure(self) -> list[dict]:
        pending = []
        for building in self.report.buildings:
            if not building.verified:
                pending.append(
                    {
                        "scope": "building",
                        "id": building.building,
                        "area": building.area,
                        "reason": "; ".join(building.reasons) or "not verified",
                        "nights_to_re_measure": sorted(
                            set(building.coverage_gap_nights)
                            | set(building.single_sensor_nights)
                            | set(building.conflicting_nights)
                        ),
                    }
                )
        for coverage in self.report.coverage:
            if not coverage.exposed_sensors:
                pending.append(
                    {
                        "scope": "area",
                        "id": coverage.area,
                        "area": coverage.area,
                        "reason": "no exposed monitor covers this area, so its buildings and "
                        "residents cannot be matched to an exposure measurement",
                        "nights_to_re_measure": list(coverage.trial_nights),
                    }
                )
        return pending

    def _withheld_by_boundary(self, candidates: list[Candidate]) -> list[dict]:
        """Compare with a prior adopted report and explain what moved.

        A correction can move a building across the agreed eligibility boundary
        after the last decision.  Proposals that depended on the old boundary
        are paused; signed commitments are not silently released — the caller
        still has to confirm any cancellation before money is freed.
        """

        if self.baseline_report is None:
            return []
        before = {b.building: b for b in self.baseline_report.buildings}
        withheld = []
        for building in self.report.buildings:
            prior = before.get(building.building)
            if prior is None:
                continue
            related = [c for c in candidates if c.building == building.building]
            if prior.verified and not building.verified:
                withheld.append(
                    {
                        "building": building.building,
                        "area": building.area,
                        "change": "verified_then_not_verified",
                        "confirmed_before": sorted(prior.confirmed_nights),
                        "confirmed_now": sorted(building.confirmed_nights),
                        "paused_proposals": sorted(
                            c.remedy
                            for c in related
                            if not c.committed and c.status in {"suspended", "deferred"}
                        ),
                        "signed_commitments_kept": sorted(
                            c.remedy for c in related if c.committed
                        ),
                        "note": (
                            "the boundary moved; new proposals pause until re-measured, "
                            "while signed commitments stay in force until a cancellation "
                            "is confirmed"
                        ),
                    }
                )
            elif not prior.verified and building.verified:
                withheld.append(
                    {
                        "building": building.building,
                        "area": building.area,
                        "change": "newly_verified",
                        "confirmed_before": sorted(prior.confirmed_nights),
                        "confirmed_now": sorted(building.confirmed_nights),
                        "paused_proposals": [],
                        "signed_commitments_kept": [],
                        "note": "the building now clears the boundary; proposals can be scored again",
                    }
                )
            elif prior.verified and building.verified:
                if sorted(prior.confirmed_nights) != sorted(building.confirmed_nights):
                    withheld.append(
                        {
                            "building": building.building,
                            "area": building.area,
                            "change": "still_verified_nights_changed",
                            "confirmed_before": sorted(prior.confirmed_nights),
                            "confirmed_now": sorted(building.confirmed_nights),
                            "paused_proposals": [],
                            "signed_commitments_kept": sorted(
                                c.remedy for c in related if c.committed
                            ),
                            "note": "still verified, but the supporting nights changed",
                        }
                    )
        return withheld
