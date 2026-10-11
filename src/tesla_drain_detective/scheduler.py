"""Run loop: at two checkpoints per night (baseline + morning), take a
NO-WAKE read of each vehicle, decide, and alert via ntfy.sh when the
overnight drain is too high.

API-cost discipline: the loop only issues a `vehicle_data` call when a
checkpoint is actually due for a vehicle (2 scheduled reads/night), and
NEVER wakes the car — a sleeping car is skipped and retried, because
waking the car to measure drain would itself cause drain. This app never
issues commands.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from .config import Settings, VehicleConfig
from .drain import DrainDecider, Decision, SendDrainAlert, VehicleDrainState
from .notifier import NotifierError, send_ntfy
from .tesla_client import TeslaApiError, TeslaClient

log = logging.getLogger("tcc-drain")


def to_drain_state(vc: VehicleConfig, data: dict | None) -> VehicleDrainState:
    """Build state from a no-wake vehicle_data response.

    `data` is None (or a non-online response) when the car is asleep —
    we return an asleep marker and NEVER wake it.
    """
    if not data or data.get("state") != "online":
        return VehicleDrainState(vin=vc.vin, name=vc.name, soc=None,
                                 sentry_mode=False, climate_keeper_mode="off",
                                 cop_state=None, parked=True, asleep=True)
    charge = data.get("charge_state", {}) or {}
    climate = data.get("climate_state", {}) or {}
    drive = data.get("drive_state", {}) or {}
    vehicle = data.get("vehicle_state", {}) or {}
    shift = drive.get("shift_state")
    return VehicleDrainState(
        vin=vc.vin,
        name=vc.name,
        soc=charge.get("battery_level"),
        sentry_mode=bool(vehicle.get("sentry_mode")),
        climate_keeper_mode=str(climate.get("climate_keeper_mode", "off")),
        cop_state=climate.get("cabin_overheat_protection"),
        parked=shift in ("P", None),
        asleep=False,
    )


def build_states(client, settings: Settings,
                 vins: list[str]) -> list[VehicleDrainState]:
    """No-wake reads, only for vehicles with a due checkpoint."""
    states = []
    by_vin = {vc.vin: vc for vc in settings.vehicles}
    for vin in vins:
        vc = by_vin[vin]
        try:
            data = client.vehicle_data(vin)  # never ensure_awake / wake_up here
        except TeslaApiError as exc:
            log.warning("%s: read failed: %s", vc.name, exc)
            continue
        st = to_drain_state(vc, data)
        if st.asleep:
            log.info("%s: asleep — skipping (never waking to measure drain)", vc.name)
        states.append(st)
    return states


def load_decider(settings: Settings) -> DrainDecider:
    decider = DrainDecider(settings)
    path = Path(settings.state_file).expanduser()
    if path.exists():
        try:
            decider.from_dict(json.loads(path.read_text()))
        except Exception as exc:
            log.warning("Could not load state file: %s", exc)
    return decider


def save_decider(decider: DrainDecider, settings: Settings) -> None:
    path = Path(settings.state_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decider.to_dict(), indent=2))


def execute(client, decider: DrainDecider, settings: Settings,
            decision: Decision, now: datetime) -> None:
    for action in decision.actions:
        if isinstance(action, SendDrainAlert):
            log.warning("DRAIN ALERT %s: %.2f%%/h (%s)",
                        action.name, action.drain_pct_per_h,
                        ", ".join(action.culprits) or "no obvious culprit")
            log.warning("  %s", action.message.replace("\n", " | "))
            if settings.dry_run:
                log.info("dry-run: alert not sent")
                decider.mark_evaluated(action.vin, now, alerted=False)
                continue
            if not settings.ntfy_topic:
                log.warning("TCC_NTFY_TOPIC not set — alert logged only, not pushed")
                decider.mark_evaluated(action.vin, now, alerted=False)
                continue
            try:
                send_ntfy(settings.ntfy_topic, action.title, action.message)
            except NotifierError as exc:
                # Don't mark evaluated: the next tick retries the send.
                log.warning("ntfy send failed for %s: %s — will retry", action.name, exc)
                continue
            decider.mark_evaluated(action.vin, now, alerted=True)
            log.info("alert pushed via ntfy.sh")
    if decision.note:
        log.info("note: %s", decision.note)


def checkpoints_due(decider: DrainDecider, settings: Settings,
                    now: datetime) -> list[str]:
    """VINs needing a no-wake read right now (baseline or morning due)."""
    return [vc.vin for vc in settings.vehicles
            if decider.baseline_due(vc.vin, now) or decider.morning_due(vc.vin, now)]


def run_once(client, decider: DrainDecider, settings: Settings,
             now: datetime | None = None) -> Decision:
    """Single sense-decide-act pass. Only reads vehicles with a due checkpoint."""
    now = now or datetime.now().astimezone()
    vins = checkpoints_due(decider, settings, now)
    states = build_states(client, settings, vins)
    # Log nights that expired without the car ever waking (state cleanup
    # happens automatically on the next night's rollover).
    for vc in settings.vehicles:
        if decider.past_grace_unresolved(vc.vin, now):
            reason = (f"{vc.name}: car never woke during the morning grace window — "
                      "night abandoned, no alert")
            decider.mark_abandoned(vc.vin, now, reason)
            log.info(reason)
    decision = decider.decide(states, now)
    execute(client, decider, settings, decision, now)
    save_decider(decider, settings)
    return decision


def run_loop(settings: Settings) -> None:
    client = TeslaClient(
        client_id=settings.client_id, client_secret=settings.client_secret,
        region=settings.region, redirect_uri=settings.redirect_uri,
        token_file=settings.token_file,
    )
    decider = load_decider(settings)
    mode = "DRY-RUN" if settings.dry_run else "LIVE"
    log.info("Tesla Drain Detective starting (%s), checking every %ds",
             mode, settings.poll_interval_seconds)
    log.info("Night window %s–%s, alert threshold %.2f%%/h",
             settings.night_start.strftime("%H:%M"), settings.night_end.strftime("%H:%M"),
             settings.drain_alert_pct_per_h)
    while True:
        try:
            run_once(client, decider, settings)
        except TeslaApiError as exc:
            log.error("Fleet API error: %s", exc)
        except Exception:
            log.exception("Unexpected error in run loop")
        time.sleep(settings.poll_interval_seconds)
