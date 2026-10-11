"""Fake fleet for testing and demos. No API calls, no real cars.

The fake car drains its battery overnight based on which culprits are
active. Drain rates are approximations of community-reported numbers:
  base vampire drain ......... 0.05 %/h
  Sentry Mode ................ +0.65 %/h
  climate keeper (Dog/Camp) .. +0.90 %/h
  Cabin Overheat Protection .. +0.15 %/h
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

BASE_DRAIN_PCT_PER_H = 0.05
SENTRY_DRAIN_PCT_PER_H = 0.65
KEEPER_DRAIN_PCT_PER_H = 0.90
COP_DRAIN_PCT_PER_H = 0.15


@dataclass
class FakeVehicle:
    vin: str
    name: str
    soc: float = 70.0
    sentry_mode: bool = False
    keeper_mode: str = "off"
    cop_state: str = "Off"
    parked: bool = True
    asleep: bool = False  # when True, vehicle_data reports state=asleep

    def drain_rate(self) -> float:
        rate = BASE_DRAIN_PCT_PER_H
        if self.sentry_mode:
            rate += SENTRY_DRAIN_PCT_PER_H
        if self.keeper_mode.lower() != "off":
            rate += KEEPER_DRAIN_PCT_PER_H
        if self.cop_state.lower() == "on":
            rate += COP_DRAIN_PCT_PER_H
        return rate

    def tick(self, minutes: float) -> None:
        if self.parked:
            self.soc = max(0.0, self.soc - self.drain_rate() * minutes / 60.0)


class FakeTeslaClient:
    """Stand-in for TeslaClient with the methods the scheduler uses.

    Deliberately has NO working wake_up: any wake attempt is recorded so
    tests can assert the app never wakes the car.
    """

    def __init__(self, vehicles: list[FakeVehicle]):
        self.vehicles = {v.vin: v for v in vehicles}
        self.data_calls = 0
        self.wake_up_calls: list[str] = []

    def vehicle_data(self, vin: str) -> dict:
        self.data_calls += 1
        v = self.vehicles[vin]
        if v.asleep:
            return {"state": "asleep"}
        return {
            "state": "online",
            "charge_state": {"battery_level": round(v.soc, 2)},
            "climate_state": {
                "climate_keeper_mode": v.keeper_mode,
                "cabin_overheat_protection": v.cop_state,
            },
            "drive_state": {"shift_state": "P" if v.parked else "D"},
            "vehicle_state": {"sentry_mode": v.sentry_mode},
        }

    def wake_up(self, vin: str) -> dict:  # must never be called
        self.wake_up_calls.append(vin)
        raise AssertionError("Drain Detective must never wake the car")

    def tick(self, minutes: float) -> None:
        for v in self.vehicles.values():
            v.tick(minutes)


def run_simulation(vehicles: list[FakeVehicle], decider, settings,
                   start: datetime, hours: float = 12.0,
                   tick_minutes: float = 10.0) -> dict:
    """Drive the real scheduler against the fake fleet over virtual time."""
    from .scheduler import run_once

    client = FakeTeslaClient(vehicles)
    now = start
    end = start + timedelta(hours=hours)
    timeline: list[dict] = []
    while now < end:
        decision = run_once(client, decider, settings, now=now)
        timeline.append({
            "time": now.strftime("%H:%M"),
            "soc": {v.name: round(v.soc, 1) for v in vehicles},
            "note": decision.note,
            "actions": [(type(a).__name__, a.name,
                         getattr(a, "drain_pct_per_h", None)) for a in decision.actions],
        })
        client.tick(tick_minutes)
        now += timedelta(minutes=tick_minutes)
    return {
        "timeline": timeline,
        "data_calls": client.data_calls,
        "wake_up_calls": client.wake_up_calls,
        "final_soc": {v.name: round(v.soc, 1) for v in vehicles},
    }
