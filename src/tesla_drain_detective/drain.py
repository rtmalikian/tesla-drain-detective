"""Decision logic: record an overnight SoC baseline, then evaluate the
morning drain rate and raise an alert (with culprit) when it is too high.

Pure logic — no network, `now` injected — so it is fully unit-testable.

Night model
-----------
A "night" starts at TCC_NIGHT_START (default 22:00) and ends at
TCC_NIGHT_END (default 07:00) the next day. The decider keeps per-vehicle
state for the current night:

1. Baseline pass — first tick inside the night window where the car is
   parked and awake (never wake the car to measure drain): record SoC.
2. Morning pass — first tick at/after night end: compare SoC, compute
   %/h. If the rate exceeds the threshold -> SendDrainAlert naming the
   likely culprits. If the car is asleep, retry until the grace window
   expires; if it never wakes, abandon the night without an alert.
3. Nights where the car charged or was driven are discarded silently —
   a rising SoC is not drain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class VehicleDrainState:
    vin: str
    name: str
    soc: float | None            # battery_level %; None when unknown/asleep
    sentry_mode: bool
    climate_keeper_mode: str     # off, Dog, Camp, On, ...
    cop_state: str | None        # On / Off / None if unknown
    parked: bool
    asleep: bool                 # True when the car wouldn't give fresh data


@dataclass
class SendDrainAlert:
    vin: str
    name: str
    title: str
    message: str
    culprits: list[str]
    drain_pct_per_h: float
    lost_pct: float
    hours: float


@dataclass
class Decision:
    actions: list = field(default_factory=list)
    note: str = ""


def identify_culprits(st: VehicleDrainState) -> list[str]:
    """Name the likely drain culprits from the vehicle's morning state."""
    culprits: list[str] = []
    if st.sentry_mode:
        culprits.append("Sentry Mode")
    keeper = (st.climate_keeper_mode or "off").strip()
    if keeper.lower() != "off":
        culprits.append(f"climate keeper ({keeper})")
    if st.cop_state is not None and st.cop_state.lower() == "on":
        culprits.append("Cabin Overheat Protection")
    return culprits


def build_alert(name: str, culprits: list[str], drain_pct_per_h: float,
                lost_pct: float, hours: float, threshold: float,
                baseline_soc: float, morning_soc: float) -> tuple[str, str]:
    title = f"\U0001f50b Drain Detective: {name} lost {lost_pct:.1f}% overnight"
    if culprits:
        culprit_line = "Likely culprits: " + ", ".join(culprits) + "."
    else:
        culprit_line = ("No obvious culprit (Sentry, climate keeper and COP were all off) — "
                        "check third-party apps polling the car or a stuck software update "
                        "keeping it awake.")
    message = (
        f"Overnight drain {drain_pct_per_h:.2f}%/h ({lost_pct:.1f}% over {hours:.1f}h) — "
        f"above your {threshold:.2f}%/h alert threshold.\n"
        f"{culprit_line}\n"
        f"Baseline {baseline_soc:.1f}% → morning {morning_soc:.1f}%."
    )
    return title, message


class DrainDecider:
    """Decides drain-baseline / morning-evaluation actions for the fleet."""

    def __init__(self, settings):
        self.s = settings
        # vin -> {"night_id": str, "baseline_soc": float, "baseline_time": iso,
        #         "baseline_culprits": [...], "evaluated": bool, "alerted": bool}
        self.nights: dict[str, dict] = {}

    # -- time helpers ----------------------------------------------------
    def night_id_for(self, now: datetime) -> str:
        """Which night does `now` belong to? Returns the date the night started."""
        if now.time() >= self.s.night_start:
            return now.date().isoformat()
        return (now - timedelta(days=1)).date().isoformat()

    def night_start_dt(self, night_id: str) -> datetime:
        d = datetime.fromisoformat(night_id)
        return d.replace(hour=self.s.night_start.hour, minute=self.s.night_start.minute,
                         second=0, microsecond=0, tzinfo=None)

    def morning_due_dt(self, night_id: str) -> datetime:
        """The morning that ends the night: night_end on the day after night_id."""
        d = datetime.fromisoformat(night_id) + timedelta(days=1)
        return d.replace(hour=self.s.night_end.hour, minute=self.s.night_end.minute,
                         second=0, microsecond=0, tzinfo=None)

    def in_night_window(self, night_id: str, now: datetime) -> bool:
        return self.night_start_dt(night_id) <= now.replace(tzinfo=None) < self.morning_due_dt(night_id)

    def baseline_due(self, vin: str, now: datetime) -> bool:
        """Should we attempt a baseline read for this VIN right now? Pure (no API)."""
        nid = self.night_id_for(now)
        st = self.nights.get(vin)
        if st and st.get("night_id") == nid and st.get("baseline_soc") is not None:
            return False
        return self.in_night_window(nid, now.replace(tzinfo=None))

    def morning_due(self, vin: str, now: datetime) -> bool:
        """Should we attempt a morning evaluation read for this VIN? Pure (no API).

        Only true inside the grace window — after that the night is
        abandoned silently and the next night's rollover cleans up.
        """
        nid = self.night_id_for(now)
        st = self.nights.get(vin)
        if not st or st.get("night_id") != nid:
            return False
        if st.get("baseline_soc") is None or st.get("evaluated"):
            return False
        due = self.morning_due_dt(nid)
        n = now.replace(tzinfo=None)
        return due <= n <= due + timedelta(hours=self.s.morning_grace_hours)

    def past_grace_unresolved(self, vin: str, now: datetime) -> bool:
        """True when a night has a baseline but was never evaluated and the
        grace window has expired — the scheduler uses this to log the
        abandonment exactly once."""
        nid = self.night_id_for(now)
        st = self.nights.get(vin)
        if not st or st.get("night_id") != nid:
            return False
        if st.get("baseline_soc") is None or st.get("evaluated"):
            return False
        return now.replace(tzinfo=None) > self.morning_due_dt(nid) + timedelta(
            hours=self.s.morning_grace_hours)

    def mark_evaluated(self, vin: str, now: datetime, alerted: bool) -> None:
        ns = self._night_state(vin, now)
        ns["evaluated"] = True
        ns["alerted"] = alerted

    def mark_abandoned(self, vin: str, now: datetime, reason: str) -> str:
        ns = self._night_state(vin, now)
        ns["evaluated"] = True
        return reason

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> dict:
        return {"nights": self.nights}

    def from_dict(self, data: dict) -> None:
        self.nights = dict(data.get("nights", {}))

    def _night_state(self, vin: str, now: datetime) -> dict:
        nid = self.night_id_for(now)
        st = self.nights.get(vin)
        if not st or st.get("night_id") != nid:
            st = {"night_id": nid, "baseline_soc": None, "baseline_time": None,
                  "baseline_culprits": [], "evaluated": False, "alerted": False}
            self.nights[vin] = st
        return st

    # -- main --------------------------------------------------------------
    def decide(self, states: list[VehicleDrainState], now: datetime) -> Decision:
        actions: list = []
        notes: list[str] = []
        now_naive = now.replace(tzinfo=None)

        for st in states:
            ns = self._night_state(st.vin, now)
            nid = ns["night_id"]
            morning_due = self.morning_due_dt(nid)

            # --- baseline pass: first parked+awake tick inside the night ----
            if ns["baseline_soc"] is None and self.in_night_window(nid, now_naive):
                if st.asleep or st.soc is None:
                    notes.append(f"{st.name}: asleep — will retry baseline")
                    continue
                if not st.parked:
                    notes.append(f"{st.name}: not parked — waiting to record baseline")
                    continue
                ns["baseline_soc"] = st.soc
                ns["baseline_time"] = now.isoformat()
                ns["baseline_culprits"] = identify_culprits(st)
                notes.append(f"{st.name}: baseline {st.soc:.1f}% recorded")
                continue

            # --- morning pass: compare against the baseline ----------------
            # (the scheduler only reads inside the grace window, so no
            # expiry handling is needed here)
            if ns["baseline_soc"] is not None and not ns["evaluated"] \
                    and now_naive >= morning_due:
                if st.asleep or st.soc is None:
                    notes.append(f"{st.name}: asleep at morning check — retrying")
                    continue
                if not st.parked:
                    ns["evaluated"] = True
                    notes.append(f"{st.name}: driven since baseline — night discarded")
                    continue
                baseline_soc = ns["baseline_soc"]
                if st.soc > baseline_soc + 0.05:
                    ns["evaluated"] = True
                    notes.append(f"{st.name}: charged overnight "
                                 f"({baseline_soc:.1f}% → {st.soc:.1f}%) — night discarded")
                    continue
                baseline_time = datetime.fromisoformat(ns["baseline_time"])
                hours = max((now - baseline_time).total_seconds() / 3600.0, 0.25)
                lost = baseline_soc - st.soc
                rate = lost / hours
                culprits = identify_culprits(st)
                if rate > self.s.drain_alert_pct_per_h:
                    title, message = build_alert(
                        st.name, culprits, rate, lost, hours,
                        self.s.drain_alert_pct_per_h, baseline_soc, st.soc)
                    actions.append(SendDrainAlert(
                        st.vin, st.name, title, message, culprits, rate, lost, hours))
                    notes.append(f"{st.name}: ALERT — {rate:.2f}%/h drain "
                                 f"({', '.join(culprits) if culprits else 'no obvious culprit'})")
                    # NOTE: evaluated is set by the scheduler AFTER the alert
                    # is actually delivered (mark_evaluated), so a failed
                    # send is retried on the next tick.
                else:
                    ns["evaluated"] = True
                    notes.append(f"{st.name}: drain normal ({rate:.2f}%/h over {hours:.1f}h)")

        return Decision(actions, note="; ".join(notes))
