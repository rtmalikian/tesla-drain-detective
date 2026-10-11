"""Push alerts through ntfy.sh — free, no account, no API key.

Publish: POST https://ntfy.sh/<topic> with the message body.
The topic is the secret: anyone who knows it can read the alerts,
so pick something unguessable (see .env.example).
"""
from __future__ import annotations

import requests


class NotifierError(Exception):
    pass


def send_ntfy(topic: str, title: str, message: str, timeout: int = 15) -> None:
    """POST a notification to ntfy.sh. Raises NotifierError on failure."""
    if not topic or "/" in topic.strip("/"):
        raise NotifierError(f"invalid ntfy topic {topic!r} (must be a single path segment)")
    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic.strip()}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8", "replace").decode("ascii", "replace"),
                "Tags": "battery",
                "Priority": "4",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise NotifierError(f"ntfy.sh request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise NotifierError(f"ntfy.sh -> {resp.status_code}: {resp.text[:200]}")
