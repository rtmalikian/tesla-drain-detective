# Tesla Drain Detective — Find Out Why Your Tesla Loses Charge Overnight

**Free, open-source app that watches your parked Tesla's battery overnight and pushes you an alert when phantom drain is too high — naming the culprit (Sentry Mode, Dog Mode / climate keeper, Cabin Overheat Protection) automatically.** Built on the official [Tesla Fleet API](https://developer.tesla.com/docs/fleet-api). Works with Model 3, Model Y, Model S, Model X, and Cybertruck.

## Why?

Every Tesla owner knows the feeling: you park at 70% in the evening and wake up to 63% — and you have **no idea where the charge went**. Was it Sentry Mode? Did you leave climate keeper on? Is Cabin Overheat Protection fighting the sun in your driveway? Or is something else quietly keeping the car awake?

Tesla gives you no overnight drain report, so owners guess — and usually guess wrong:

- **Sentry Mode is the #1 phantom-drain culprit** (~0.7%/h — that's 6%+ overnight), and it's easy to leave on at home without noticing
- **Climate keeper (Dog/Camp Mode) left on** drains ~1%/h and looks identical to "vampire drain" in the morning
- **Cabin Overheat Protection** cycles the AC all day in a sunny driveway
- **Third-party apps polling your car** keep it from sleeping — invisible in the Tesla app

Drain Detective answers the question every morning with data instead of guesses: it records your battery level at night, checks it again in the morning, and if the drain rate is above your threshold, it tells you **what was on** while it happened — pushed straight to your phone via a free ntfy.sh alert.

## How it works

```
22:00  Baseline: Model Y at 71.0% (Sentry Mode ON)
  ...  Car sleeps. The app does nothing — it NEVER wakes the car to
       measure drain, because waking it would cause the drain it's
       trying to measure.
07:00  Morning check: Model Y at 64.4% → 0.70%/h over 9h
07:00  ⚡ Push alert: "Model Y lost 6.6% overnight (0.70%/h).
       Likely culprits: Sentry Mode."
```

The app runs as a lightweight daemon that only touches the API at **two scheduled checkpoints per night** — a baseline read after your configured night-start time, and a morning read after night-end. Between checkpoints it makes zero API calls. If the car is asleep at a checkpoint, the read is skipped and retried later — waking the car is the most expensive Fleet API call and would corrupt the very measurement being taken.

**Expected API cost:** ~$0.10–0.30/month at the default schedule (2 data reads/night, 0 commands, 0 wakes) — comfortably inside Tesla's $10/month developer credit, so effectively $0.

Nights where the car charged or was driven are discarded automatically (a rising battery isn't drain), and each alert fires at most once per night.

## Quickstart

```bash
git clone https://github.com/rtmalikian/tesla-drain-detective.git
cd tesla-drain-detective
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Configure (secrets stay local — .env is git-ignored)
cp .env.example .env
nano .env   # Tesla API keys, VINs, drain threshold, ntfy topic

# 2. Authorize with your Tesla account (one-time)
tcc-drain auth

# 3. Install the virtual key on each car (one-time; reads still need it)
tcc-drain pairing

# 4. Watch it work on a fake night — no API calls, no real cars
tcc-drain simulate

# 5. Single safe pass against your real cars (read-only, never wakes)
TCC_DRY_RUN=true tcc-drain run --once

# 6. Run the daemon (e.g. under systemd or cron @reboot)
tcc-drain run
```

Get push alerts on your phone: install the free [ntfy app](https://ntfy.sh) (iOS/Android), pick a secret topic like `TCC_NTFY_TOPIC=$(openssl rand -hex 12)`, subscribe to it in the app, and put it in your `.env`. No account, no API key, no cost.

## Tesla developer setup (one-time, ~15 minutes)

**1. Register a developer app** at [developer.tesla.com](https://developer.tesla.com) and request these scopes:

- `vehicle_device_data` — read battery level, Sentry/climate/COP state
- `vehicle_location` — (reserved for future geofenced thresholds)
- `vehicle_cmds` — required by Tesla even for read-only apps

**2. Run `tcc-drain auth`** and sign in with the Tesla account that owns the cars.

**3. Install the app's virtual key** on each car (`tcc-drain pairing` prints the steps): host your public key,
register it via `POST /api/1/partner_accounts`, then approve it at `https://www.tesla.com/_ak/<your-domain>`
from each car's touchscreen.

## Configuration (`.env` reference)

| Variable | Default | What it does |
|---|---|---|
| `TESLA_CLIENT_ID` / `TESLA_CLIENT_SECRET` | — | From developer.tesla.com |
| `TESLA_REGION` | `na` | `na`, `eu`, or `cn` |
| `TCC_VEHICLES` | — | `name=VIN,...` — supports multiple Teslas |
| `TCC_DRAIN_ALERT_PCT_PER_H` | `0.5` | Alert when overnight drain exceeds this %/h |
| `TCC_NIGHT_START` / `TCC_NIGHT_END` | `22:00` / `07:00` | Night window (baseline → morning check) |
| `TCC_MORNING_GRACE_HOURS` | `4` | Keep retrying the morning read this long if the car is asleep |
| `TCC_NTFY_TOPIC` | — | Secret [ntfy.sh](https://ntfy.sh) topic for push alerts (free, no account) |
| `TCC_STATE_FILE` | `~/.tcc/drain_detective_state.json` | Where nightly baselines are kept |
| `TCC_POLL_INTERVAL_SECONDS` | `600` | How often the daemon checks whether a checkpoint is due |
| `TCC_DRY_RUN` | `false` | Log alerts without pushing them |

## Try before you connect: `tcc-drain simulate`

No Tesla account needed — simulates one night with two fake cars (one with Sentry Mode on, one quiet):

```
21:30 → 08:30: Model Y has Sentry Mode ON, Model 3 is quiet.

22:00  Model Y: baseline 70.7% recorded; Model 3: baseline 64.0% recorded
07:00  Model Y: ALERT — 0.70%/h drain (Sentry Mode); Model 3: drain normal

Model Y alert fired: yes (PASS)
Model 3 alert fired: no (PASS)
wake_up calls: 0 (PASS — never wakes)
vehicle_data calls for the night: 4
Estimated API cost: ~$0.24/month (4 data calls/night, 0 commands, 0 wakes)
```

## FAQ

**Why is my Tesla losing charge overnight?**
The usual suspects in order: Sentry Mode (~0.7%/h), climate keeper / Dog Mode left on (~1%/h), Cabin Overheat Protection on a hot day, or a third-party app polling the car and preventing sleep. This app tells you which one it was — with numbers.

**How much phantom drain is normal?**
A healthy, sleeping Tesla loses roughly 0.05–0.2% per hour (~1–2% overnight). Anything consistently above ~0.5%/h means something is keeping the car awake, and that's the default alert threshold here.

**Does checking the battery drain the battery?**
It can — if the check wakes the car. That's why Drain Detective **never wakes your car**: if it's asleep at a checkpoint, the read is skipped and retried later. The measurement never causes the drain it's measuring.

**Why not just use Tessie or TeslaFi?**
They're great, but they're paid subscriptions ($5–13/month) and cloud services holding your car's API access. This is free, open source, runs on your own machine, and costs ~$0.20/month in Fleet API usage.

**What does it cost to run?**
Tesla's Fleet API is pay-per-use with a $10/month developer credit per account. Two data reads per night and zero commands/wakes lands around $0.10–0.30/month — inside the free credit, so effectively $0.

**What if my car is asleep all night?**
Then there's no baseline and no alert — the night is skipped quietly. A sleeping car barely drains anyway, which is exactly what you want to see.

**Multiple Teslas?**
Yes — list them all in `TCC_VEHICLES`; each car gets its own nightly baseline and its own alerts.

## Development

```bash
PYTHONPATH=src python -m pytest tests/ -q   # 33 tests incl. full-night simulation
```

## License

MIT — see [LICENSE](LICENSE).
