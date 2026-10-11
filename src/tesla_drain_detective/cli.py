"""CLI: tcc-drain auth | status | run | once | simulate | pairing"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from . import __version__
from .config import ConfigError, Settings, VehicleConfig, load_settings
from .drain import DrainDecider
from .scheduler import load_decider, run_loop, run_once
from .simulator import FakeVehicle, run_simulation
from .tesla_client import TeslaApiError, TeslaClient

log = logging.getLogger("tcc-drain")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")


def _client(settings: Settings) -> TeslaClient:
    return TeslaClient(client_id=settings.client_id, client_secret=settings.client_secret,
                       region=settings.region, redirect_uri=settings.redirect_uri,
                       token_file=settings.token_file)


def cmd_auth(args, settings: Settings) -> int:
    client = _client(settings)
    url = client.authorize_url(state="tccdrain")
    print("1. Open this URL and sign in with your Tesla account:\n\n   %s\n" % url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print("2. After sign-in you'll land on a localhost URL that fails to load.")
    print("   Paste that FULL redirected URL here (it contains ?code=...).")
    redirected = input("Redirected URL: ").strip()
    if "code=" not in redirected:
        print("ERROR: no ?code= found.", file=sys.stderr)
        return 1
    try:
        client.exchange_code(redirected.split("code=")[1].split("&")[0])
    except TeslaApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"\nAuthorized! Tokens saved to {settings.token_file} (mode 600).")
    print("Next: run `tcc-drain pairing` for the one-time virtual-key setup.")
    return 0


def cmd_pairing(_args, _settings: Settings) -> int:
    print(
        "One-time virtual-key setup (required before commands work):\n\n"
        "  1. Host your app's public key at\n"
        "     https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem\n"
        "  2. Register it: POST /api/1/partner_accounts {\"domain\": \"<your-domain>\"}\n"
        "  3. In EACH Tesla, open https://www.tesla.com/_ak/<your-domain>\n"
        "     and approve the key on the touchscreen.\n"
        "\n"
        "Note: Drain Detective only READS data (vehicle_data) — it never\n"
        "sends commands — but the Fleet API still requires a registered key.\n"
    )
    return 0


def cmd_status(_args, settings: Settings) -> int:
    path = Path(settings.state_file).expanduser()
    nights = {}
    if path.exists():
        try:
            nights = json.loads(path.read_text()).get("nights", {})
        except Exception as exc:
            print(f"State file unreadable: {exc}")
    print(f"Night window: {settings.night_start.strftime('%H:%M')}–"
          f"{settings.night_end.strftime('%H:%M')}  |  "
          f"alert threshold: {settings.drain_alert_pct_per_h:.2f}%/h  |  "
          f"ntfy: {'set' if settings.ntfy_topic else 'NOT SET'}")
    print(f"{'Vehicle':<12}{'Night':<12}{'Baseline':<10}{'Evaluated':<10}{'Alerted':<8}")
    print("-" * 54)
    for vc in settings.vehicles:
        ns = nights.get(vc.vin, {})
        base = ns.get("baseline_soc")
        print(f"{vc.name:<12}{ns.get('night_id', '—'):<12}"
              f"{(f'{base:.1f}%' if base is not None else '—'):<10}"
              f"{str(bool(ns.get('evaluated'))):<10}"
              f"{str(bool(ns.get('alerted'))):<8}")
    if not settings.ntfy_topic:
        print("\nTip: set TCC_NTFY_TOPIC to a secret ntfy.sh topic to get push alerts.")
    return 0


def cmd_run(args, settings: Settings) -> int:
    if args.once:
        run_once(_client(settings), load_decider(settings), settings)
        return 0
    run_loop(settings)
    return 0


def cmd_simulate(_args, _settings: Settings) -> int:
    """Demo night: one car with Sentry on (alert fires), one quiet car (no alert)."""
    start = datetime.now().astimezone().replace(hour=21, minute=30, second=0, microsecond=0)
    vehicles = [
        FakeVehicle(vin="SIM1", name="Model Y", soc=71.0, sentry_mode=True),
        FakeVehicle(vin="SIM2", name="Model 3", soc=64.0),
    ]
    settings = Settings(
        vehicles=[VehicleConfig(name="Model Y", vin="SIM1"),
                  VehicleConfig(name="Model 3", vin="SIM2")],
        drain_alert_pct_per_h=0.5,
        ntfy_topic="",  # simulate logs the alert instead of pushing
        dry_run=True,
    )
    decider = DrainDecider(settings)
    report = run_simulation(vehicles, decider, settings, start, hours=11.0)

    print("\nSimulating one night (21:30 → 08:30): Model Y has Sentry Mode ON, "
          "Model 3 is quiet.\n")
    print(f"{'Time':<8}{'Model Y %':<11}{'Model 3 %':<11}Note")
    print("-" * 80)
    last = None
    for tick in report["timeline"]:
        key = (tick["note"], tuple(tick["soc"].values()))
        if tick["note"] != last:
            print(f"{tick['time']:<8}{tick['soc']['Model Y']:<11}"
                  f"{tick['soc']['Model 3']:<11}{tick['note'][:52]}")
            last = tick["note"]
    alerts = [a for t in report["timeline"] for a in t["actions"]
              if a[0] == "SendDrainAlert"]
    y_alerts = [a for a in alerts if a[1] == "Model Y"]
    m3_alerts = [a for a in alerts if a[1] == "Model 3"]
    print(f"\nModel Y alert fired: {'yes' if y_alerts else 'NO'} "
          f"({'PASS' if y_alerts else 'FAIL — bug!'})")
    if y_alerts:
        print(f"  drain rate: {y_alerts[0][2]:.2f}%/h (threshold 0.50%/h)")
    print(f"Model 3 alert fired: {'yes — BUG!' if m3_alerts else 'no'} "
          f"({'FAIL — bug!' if m3_alerts else 'PASS'})")
    print(f"wake_up calls: {len(report['wake_up_calls'])} "
          f"({'PASS — never wakes' if not report['wake_up_calls'] else 'FAIL — bug!'})")
    print(f"vehicle_data calls for the night: {report['data_calls']}")
    # API cost: ~2 scheduled data reads/night, 0 commands, 0 wakes
    monthly = report["data_calls"] * 30 / 500.0  # $1 per 500 data calls
    print(f"Estimated API cost: ~${monthly:.2f}/month "
          f"({report['data_calls']} data calls/night, 0 commands, 0 wakes)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tcc-drain",
                                description="Overnight phantom-drain alerts for your Tesla.")
    p.add_argument("--version", action="version", version=f"tcc-drain {__version__}")
    p.add_argument("--env-file", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="Authorize with your Tesla account (one-time)")
    sub.add_parser("pairing", help="Virtual-key pairing instructions")
    sub.add_parser("status", help="Show last night's drain check per vehicle")
    run_p = sub.add_parser("run", help="Run the drain watcher (daemon)")
    run_p.add_argument("--once", action="store_true", help="Single pass, then exit")
    sub.add_parser("simulate", help="Demo one night against fake cars (no API calls)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    load_dotenv(args.env_file)
    try:
        settings = load_settings(require_auth=args.command in ("auth", "status", "run"))
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    return {
        "auth": cmd_auth, "pairing": cmd_pairing, "status": cmd_status,
        "run": cmd_run, "simulate": cmd_simulate,
    }[args.command](args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
