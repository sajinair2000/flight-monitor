#!/usr/bin/env python3
"""
Round-trip flight price monitor (one or more departure airports).

Checks Google Flights (via SerpAPI) once per run for each origin airport,
compares with the last run, and emails you ONLY when a price has dropped.
Notification = email (Gmail) if EMAIL_USER is set, else SMS (Twilio) if set,
else printed to the screen.

Required env vars:
  SERPAPI_KEY
  For email alerts: EMAIL_USER (your Gmail address), EMAIL_APP_PASSWORD,
                    ALERT_EMAIL (optional, defaults to EMAIL_USER)
Optional env vars:
  ORIGIN=JFK,EWR   (one or more airport codes, comma-separated)
  DEST=BLR  DEPART=2027-01-01  RETURN=2027-01-10  CURRENCY=USD
  MAX_STOPS=1 (0 = nonstop only, 1 = at most one stop, 2 = at most two)
  MIN_DROP=0 (smallest drop, in currency units, that triggers an alert)
  TEST_EMAIL=1 (just send a test alert and exit)
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import requests

ORIGINS = [o.strip().upper() for o in os.getenv("ORIGIN", "JFK,EWR").split(",") if o.strip()]
DEST = os.getenv("DEST", "BLR").strip().upper()
DEPART = os.getenv("DEPART", "2027-01-01")
RETURN = os.getenv("RETURN", "2027-01-10")
CURRENCY = os.getenv("CURRENCY", "USD")
MIN_DROP = float(os.getenv("MIN_DROP", "0"))
MAX_STOPS = int(os.getenv("MAX_STOPS", "1"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))


def send_email(subject: str, body: str) -> None:
    user = os.environ["EMAIL_USER"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = os.getenv("ALERT_EMAIL") or user
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, os.environ["EMAIL_APP_PASSWORD"].replace(" ", ""))
        smtp.send_message(msg)
    print("Email sent:", subject)


def send_sms(body: str) -> None:
    if not os.getenv("TWILIO_SID"):  # Twilio not set up: just print the alert
        print("ALERT (no email/SMS configured):\n" + body)
        return
    sid, token = os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"]
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, token),
        data={"From": os.environ["TWILIO_FROM"], "To": os.environ["ALERT_TO"], "Body": body[:1500]},
        timeout=30,
    )
    r.raise_for_status()
    print("SMS sent:", body.replace("\n", " | "))


def notify(subject: str, body: str) -> None:
    if os.getenv("EMAIL_USER"):
        send_email(subject, body)
    else:
        send_sms(subject + "\n" + body)


def fetch_cheapest(origin: str) -> dict | None:
    """Return the cheapest round-trip offer from `origin`, or None if none listed."""
    r = requests.get(
        "https://serpapi.com/search.json",
        params={
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": DEST,
            "outbound_date": DEPART,
            "return_date": RETURN,
            "type": 1,  # round trip
            "adults": 1,
            "currency": CURRENCY,
            "hl": "en",
            "stops": min(MAX_STOPS + 1, 3),
            "api_key": os.environ["SERPAPI_KEY"],
        },
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    offers = [
        o
        for o in data.get("best_flights", []) + data.get("other_flights", [])
        if "price" in o and len(o.get("layovers", [])) <= MAX_STOPS
    ]
    if not offers:
        return None
    best = min(offers, key=lambda o: o["price"])
    legs = best.get("flights", [])
    airlines = sorted({f.get("airline", "?") for f in legs})
    layovers = [l.get("id") or l.get("name", "?") for l in best.get("layovers", [])]
    return {
        "price": float(best["price"]),
        "airlines": ", ".join(airlines),
        "stops": len(layovers),
        "via": "/".join(layovers),
        "hours": round(best.get("total_duration", 0) / 60, 1),
        "link": data.get("search_metadata", {}).get("google_flights_url", ""),
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def describe(o: dict) -> str:
    stops = "nonstop" if o["stops"] == 0 else f"{o['stops']} stop(s) via {o['via']}"
    return f"{o['airlines']}, {stops}, ~{o['hours']}h outbound"


def check_route(origin: str, state: dict, now: str) -> bool:
    """Check one origin. Returns False if the lookup failed."""
    route = f"{origin}<>{DEST} {DEPART} to {RETURN}"
    key = f"{origin}-{DEST}-{DEPART}-{RETURN}"  # separate saved price per route/dates
    routes = state.setdefault("routes", {})

    try:
        offer = fetch_cheapest(origin)
    except Exception as e:  # network / API failure: stay quiet, just log
        print(f"{route}: fetch failed: {e}", file=sys.stderr)
        return False

    if offer is None:
        print(f"[{now}] {route}: no flights listed")
        return True

    price = offer["price"]
    last = routes.get(key, {}).get("last_price")

    if last is not None and price < last and last - price > MIN_DROP:
        notify(
            f"Flight price drop {origin}-{DEST}: {CURRENCY} {last:.0f} -> {price:.0f}",
            f"{route}\n"
            f"{CURRENCY} {last:.0f} -> {price:.0f} (-{last - price:.0f})\n"
            f"{describe(offer)}\n{offer['link']}",
        )
    else:
        print(f"[{now}] {route}: {price:.0f} (last {last}) - no drop")
        print(f"  Cheapest option: {describe(offer)}")
        print(f"  Check it on Google Flights: {offer['link']}")

    routes[key] = {"last_price": price, "last_checked": now}
    return True


def main() -> int:
    if os.getenv("TEST_EMAIL"):  # one-off check that notifications work
        notify("Test: flight monitor alerts work", f"If you can read this, alerts for {'/'.join(ORIGINS)}-{DEST} are set up.")
        return 0
    state = load_state()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = [check_route(o, state, now) for o in ORIGINS]
    save_state(state)
    return 0 if any(results) else 1  # fail the run only if every lookup failed


if __name__ == "__main__":
    sys.exit(main())
