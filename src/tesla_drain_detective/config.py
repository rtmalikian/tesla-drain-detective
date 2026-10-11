"""Configuration for Tesla Drain Detective. All settings come from the
environment (see .env.example). Copy .env.example to .env — .env is
git-ignored so secrets never leave your machine."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time


class ConfigError(Exception):
    pass


@dataclass
class VehicleConfig:
    name: str
    vin: str


@dataclass
class Settings:
    client_id: str = ""
    client_secret: str = ""
    region: str = "na"
    redirect_uri: str = "http://localhost:8080/callback"
    token_file: str = "~/.tcc/tokens.json"
    vehicles: list[VehicleConfig] = field(default_factory=list)
    drain_alert_pct_per_h: float = 0.5
    ntfy_topic: str = ""
    night_start: time = field(default_factory=lambda: time(22, 0))
    night_end: time = field(default_factory=lambda: time(7, 0))
    morning_grace_hours: float = 4.0
    state_file: str = "~/.tcc/drain_detective_state.json"
    poll_interval_seconds: int = 600
    dry_run: bool = False


def _parse_vehicles(raw: str) -> list[VehicleConfig]:
    """Format: name=VIN,name2=VIN2 (or bare VINs)."""
    vehicles = []
    for idx, item in enumerate(raw.split(",")):
        item = item.strip()
        if not item:
            continue
        name = None
        if "=" in item:
            name, item = item.split("=", 1)
            name = name.strip()
        vin = item.strip()
        if not vin:
            raise ConfigError("Empty VIN in TCC_VEHICLES")
        vehicles.append(VehicleConfig(name=name or f"car-{idx + 1}", vin=vin))
    if not vehicles:
        raise ConfigError("TCC_VEHICLES is empty — add at least one name=VIN entry")
    return vehicles


def _float(env: dict, key: str, default: float | None) -> float | None:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def _int(env: dict, key: str, default: int) -> int:
    try:
        return int(env.get(key, default))
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{key} must be an integer") from exc


def _bool(env: dict, key: str, default: bool) -> bool:
    return env.get(key, "true" if default else "false").strip().lower() in ("1", "true", "yes", "on")


def _time(env: dict, key: str, default: time) -> time:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        hh, mm = raw.split(":")
        parsed = time(int(hh), int(mm))
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{key} must be HH:MM (24h), got {raw!r}") from exc
    if not (0 <= parsed.hour <= 23 and 0 <= parsed.minute <= 59):
        raise ConfigError(f"{key} must be HH:MM (24h), got {raw!r}")
    return parsed


def load_settings(env: dict | None = None, require_auth: bool = True) -> Settings:
    env = env if env is not None else os.environ

    client_id = env.get("TESLA_CLIENT_ID", "").strip()
    client_secret = env.get("TESLA_CLIENT_SECRET", "").strip()
    if require_auth and (not client_id or not client_secret):
        raise ConfigError("TESLA_CLIENT_ID and TESLA_CLIENT_SECRET are required (see README).")

    region = env.get("TESLA_REGION", "na").strip().lower()
    if region not in ("na", "eu", "cn"):
        raise ConfigError(f"TESLA_REGION must be na/eu/cn, got {region!r}")

    vehicles_raw = env.get("TCC_VEHICLES", "").strip()
    vehicles = _parse_vehicles(vehicles_raw) if (require_auth or vehicles_raw) else []

    alert_rate = _float(env, "TCC_DRAIN_ALERT_PCT_PER_H", 0.5)
    if alert_rate is None or alert_rate <= 0:
        raise ConfigError("TCC_DRAIN_ALERT_PCT_PER_H must be a positive number")

    grace = _float(env, "TCC_MORNING_GRACE_HOURS", 4.0)
    if grace is None or grace <= 0:
        raise ConfigError("TCC_MORNING_GRACE_HOURS must be a positive number")

    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        region=region,
        redirect_uri=env.get("TESLA_REDIRECT_URI", "http://localhost:8080/callback").strip(),
        token_file=env.get("TESLA_TOKEN_FILE", "~/.tcc/tokens.json").strip(),
        vehicles=vehicles,
        drain_alert_pct_per_h=alert_rate,
        ntfy_topic=env.get("TCC_NTFY_TOPIC", "").strip(),
        night_start=_time(env, "TCC_NIGHT_START", time(22, 0)),
        night_end=_time(env, "TCC_NIGHT_END", time(7, 0)),
        morning_grace_hours=grace,
        state_file=env.get("TCC_STATE_FILE", "~/.tcc/drain_detective_state.json").strip(),
        poll_interval_seconds=_int(env, "TCC_POLL_INTERVAL_SECONDS", 600),
        dry_run=_bool(env, "TCC_DRY_RUN", False),
    )
