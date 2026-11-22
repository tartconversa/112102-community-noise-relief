"""Local, append-only ledger for decisions, commitments and acceptances.

The source CSV files are the immutable evidence base.  Everything an operator
does in a meeting — register a correction, sign a commitment, cancel one,
accept insulation work, submit a budget change, adopt an analysis snapshot —
is appended here and can be replayed.  Source ``remedy_events`` rows are the
starting history; the ledger never rewrites them, it only adds later entries.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import StoreError, WorkflowError
from .sources import Dataset

DEFAULT_STATE_PATH = Path(".noise_state") / "state.json"
STATE_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "corrections": [],
        "snapshots": [],
        "commitments": [],
        "acceptances": [],
        "budget_adjustments": [],
        "audit": [],
        "counters": {},
    }


class Store:
    """A small JSON-backed ledger with atomic writes."""

    def __init__(self, path: str | Path = DEFAULT_STATE_PATH):
        self.path = Path(path)
        self.state = _empty_state()
        if self.path.exists():
            self._load()
        else:
            self.state["commitments"] = []
            self.state["acceptances"] = []

    # -- persistence ---------------------------------------------------
    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError(f"cannot read ledger {self.path}: {exc}") from exc
        if payload.get("version") != STATE_VERSION:
            raise StoreError(
                f"ledger {self.path} has version {payload.get('version')}, expected {STATE_VERSION}"
            )
        state = _empty_state()
        state.update(payload)
        self.state = state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True)
        handle, temp_path = tempfile.mkstemp(
            dir=self.path.parent, prefix=".state-", suffix=".json", text=True
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    # -- audit ---------------------------------------------------------
    def _next_id(self, prefix: str) -> str:
        counter = self.state.setdefault("counters", {})
        if prefix not in counter:
            # Repair ledgers written before per-prefix counters existed.
            counter[prefix] = self._highest_existing(prefix)
        counter[prefix] = counter.get(prefix, 0) + 1
        return f"{prefix}{counter[prefix]:03d}"

    def _highest_existing(self, prefix: str) -> int:
        highest = 0
        sources: list[str] = [
            entry.get("id", "") for entry in self.state.get("audit", [])
        ]
        sources += [
            snapshot.get("id", "") for snapshot in self.state.get("snapshots", [])
        ]
        sources += [
            row.get("revision", "") for row in self.state.get("corrections", [])
        ]
        for value in sources:
            if value.startswith(prefix) and value[len(prefix):].isdigit():
                highest = max(highest, int(value[len(prefix):]))
        return highest

    def _audit(self, action: str, detail: dict[str, Any]) -> str:
        entry = {
            "id": self._next_id("E"),
            "at": _now(),
            "action": action,
            "detail": detail,
        }
        self.state["audit"].append(entry)
        return entry["id"]

    # -- corrections ---------------------------------------------------
    def add_correction(
        self,
        sensor: str,
        night: int,
        corrected_db: float,
        learned_day: int,
        reason: str,
        revision: str | None = None,
    ) -> dict[str, Any]:
        for row in self.state["corrections"]:
            if row["sensor"] == sensor and row["night"] == night:
                raise StoreError(
                    f"a correction for {sensor}/night {night} is already registered as {row['revision']}"
                )
        revision = revision or self._next_id("L")
        row = {
            "revision": revision,
            "sensor": sensor,
            "night": night,
            "corrected_db": float(corrected_db),
            "learned_day": int(learned_day),
            "reason": reason,
            "registered_at": _now(),
        }
        self.state["corrections"].append(row)
        self._audit("correction", row)
        return row

    def corrections(self) -> list[dict[str, Any]]:
        return list(self.state["corrections"])

    # -- snapshots -----------------------------------------------------
    def add_snapshot(self, label: str, report: dict[str, Any]) -> dict[str, Any]:
        snapshot = {
            "id": self._next_id("S"),
            "label": label,
            "adopted_at": _now(),
            "asof_day": report["asof_day"],
            "digest": report["digest"],
            "params": report["params"],
            "report": report,
        }
        self.state["snapshots"].append(snapshot)
        self._audit("snapshot_adopted", {"id": snapshot["id"], "label": label, "digest": report["digest"]})
        return snapshot

    def snapshots(self) -> list[dict[str, Any]]:
        return list(self.state["snapshots"])

    def snapshot(self, snapshot_id: str) -> dict[str, Any]:
        for snapshot in self.state["snapshots"]:
            if snapshot["id"] == snapshot_id:
                return snapshot
        raise StoreError(f"unknown snapshot {snapshot_id}")

    def latest_snapshot(self) -> dict[str, Any] | None:
        return self.state["snapshots"][-1] if self.state["snapshots"] else None

    # -- commitments ---------------------------------------------------
    def add_commitment(
        self,
        remedy: str,
        day: int,
        operator: str,
        cents: int,
        units: float,
        note: str = "",
    ) -> dict[str, Any]:
        current = self.commitment(remedy)
        if current and current["state"] == "signed":
            raise WorkflowError(f"{remedy} already has a signed commitment")
        row = {
            "remedy": remedy,
            "operator": operator,
            "day": int(day),
            "cents": int(cents),
            "units": float(units),
            "state": "signed",
            "cancelled_day": None,
            "cancelled_confirmed": False,
            "note": note,
            "recorded_at": _now(),
        }
        self.state["commitments"].append(row)
        self._audit("commitment_signed", row)
        return row

    def request_cancel(self, remedy: str, day: int, reason: str) -> dict[str, Any]:
        current = self.commitment(remedy)
        if current is None or current["state"] != "signed":
            raise WorkflowError(f"{remedy} has no signed commitment to cancel")
        if current["cancelled_day"] is not None:
            raise WorkflowError(f"{remedy} cancellation was already requested")
        current["cancelled_day"] = int(day)
        current["state"] = "cancel_requested"
        current["cancel_reason"] = reason
        self._audit("cancel_requested", {"remedy": remedy, "day": day, "reason": reason})
        return current

    def confirm_cancel(self, remedy: str, day: int) -> dict[str, Any]:
        current = self.commitment(remedy)
        if current is None or current["state"] != "cancel_requested":
            raise WorkflowError(
                f"{remedy} has no confirmed-pending cancellation; funds stay reserved"
            )
        current["state"] = "cancelled"
        current["cancelled_confirmed"] = True
        current["confirmed_day"] = int(day)
        self._audit("cancel_confirmed", {"remedy": remedy, "day": day})
        return current

    def commitments(self) -> list[dict[str, Any]]:
        return list(self.state["commitments"])

    def commitment(self, remedy: str) -> dict[str, Any] | None:
        rows = [row for row in self.state["commitments"] if row["remedy"] == remedy]
        return rows[-1] if rows else None

    # -- acceptances ---------------------------------------------------
    def add_acceptance(
        self,
        remedy: str,
        building: str | None,
        units: float,
        day: int,
        nights: list[int] | None = None,
        note: str = "",
        prior: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        # ``prior`` carries source-recorded acceptances so the "one operator per
        # building" and "one night, one quota" checks also cover history the
        # ledger did not write itself.
        existing = [*(prior or []), *self.state["acceptances"]]
        # One building cannot be claimed as accepted by two different remedies
        # (each remedy belongs to one operator).  The same remedy may top up its
        # own acceptance for the building it already covers.
        if building is not None:
            for row in existing:
                if row.get("building") == building and row["remedy"] != remedy:
                    raise StoreError(
                        f"building {building} is already accepted under {row['remedy']}; "
                        "a second operator cannot claim the same building as delivered"
                    )
        # A reduction night may not be used to discharge two people's quotas.
        for night in nights or []:
            for row in existing:
                if night in (row.get("nights") or []) and row["remedy"] != remedy:
                    raise StoreError(
                        f"night {night} is already claimed by {row['remedy']} and cannot "
                        "also discharge this remedy"
                    )
        row = {
            "remedy": remedy,
            "building": building,
            "units": float(units),
            "nights": sorted(int(n) for n in (nights or [])),
            "day": int(day),
            "note": note,
            "recorded_at": _now(),
        }
        self.state["acceptances"].append(row)
        self._audit("acceptance", row)
        return row

    def acceptances(self) -> list[dict[str, Any]]:
        return list(self.state["acceptances"])

    # -- budget --------------------------------------------------------
    def add_budget_adjustment(self, area: str, delta_cents: int, day: int, reason: str) -> dict[str, Any]:
        row = {
            "area": area,
            "delta_cents": int(delta_cents),
            "day": int(day),
            "reason": reason,
            "recorded_at": _now(),
        }
        self.state["budget_adjustments"].append(row)
        self._audit("budget_adjustment", row)
        return row

    def budget_adjustments(self) -> list[dict[str, Any]]:
        return list(self.state["budget_adjustments"])

    # -- combined history ---------------------------------------------
    def effective_commitments(self, dataset: Dataset) -> dict[str, dict[str, Any]]:
        """Merge source ``signed`` events with the ledger.

        Source events are the immutable starting history; the ledger may add a
        later signed commitment or override the state with a cancellation.
        """

        merged: dict[str, dict[str, Any]] = {}
        for event in dataset.events:
            if event.state == "signed":
                offer = dataset.offers[event.remedy]
                merged[event.remedy] = {
                    "remedy": event.remedy,
                    "operator": offer.operator,
                    "day": event.day,
                    "cents": offer.cents,
                    "units": offer.units,
                    "state": "signed",
                    "cancelled_day": None,
                    "cancelled_confirmed": False,
                    "note": f"source event {event.event}",
                    "source_event": event.event,
                }
        for row in self.state["commitments"]:
            base = merged.get(row["remedy"], {})
            merged[row["remedy"]] = {**base, **row}
        return merged

    def effective_acceptances(self, dataset: Dataset) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event in dataset.events:
            if event.state == "partial_acceptance":
                rows.append(
                    {
                        "remedy": event.remedy,
                        "building": event.actual_building,
                        "units": float(event.actual_units),
                        "nights": [],
                        "day": event.day,
                        "note": f"source event {event.event}",
                        "source_event": event.event,
                    }
                )
        rows.extend(self.state["acceptances"])
        return rows

    def effective_budgets(self, dataset: Dataset) -> dict[str, int]:
        funding = {area: budget.funding_cents for area, budget in dataset.budgets.items()}
        for row in self.state["budget_adjustments"]:
            funding[row["area"]] = funding.get(row["area"], 0) + row["delta_cents"]
        return funding

    def audit_log(self) -> list[dict[str, Any]]:
        return list(self.state["audit"])
