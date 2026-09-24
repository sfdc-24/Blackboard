"""Run one bounded Studio voice-cleanup pass from a Cloud Run Job.

The scheduler starts the job with Google OAuth. The job, not Cloud Scheduler,
holds the maintenance credential through a Secret Manager environment binding.
No credential or controller response body is ever printed.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx


Post = Callable[..., Any]


def _configuration(url: str | None, secret: str | None) -> tuple[str, str]:
    target = (url if url is not None else os.environ.get("VOICE_SWEEP_URL", "")).strip()
    credential = secret if secret is not None else os.environ.get("STUDIO_MAINTENANCE_SECRET", "")
    parsed = urlsplit(target)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path != "/v1/maintenance/voice-sweep"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError("VOICE_SWEEP_URL must be the exact HTTPS maintenance endpoint")
    if len(credential.encode("utf-8")) < 32:
        raise RuntimeError("STUDIO_MAINTENANCE_SECRET must contain at least 32 bytes")
    return target, credential


def run_once(*, url: str | None = None, secret: str | None = None,
             post: Post | None = None) -> dict:
    """Call the maintenance endpoint once and fail closed on any ambiguity."""
    target, credential = _configuration(url, secret)
    sender = post or httpx.post
    try:
        response = sender(
            target,
            headers={"Authorization": "Bearer " + credential},
            timeout=20.0,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        # Do not chain the provider exception into the uncaught job traceback:
        # third-party exception text is not part of this job's logging contract.
        raise RuntimeError("voice sweep transport failed") from None
    status = int(getattr(response, "status_code", 0))
    if status != 200:
        raise RuntimeError("voice sweep was refused with HTTP %d" % status)
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - an invalid success body is a failed job
        raise RuntimeError("voice sweep returned an invalid success body") from None
    if (
        not isinstance(body, dict)
        or body.get("ok") is not True
        or body.get("index_available") is not True
        or not isinstance(body.get("pending"), int)
        or body["pending"] != 0
    ):
        raise RuntimeError("voice sweep did not confirm completion")
    print("voice sweep accepted")
    return body


def main() -> int:
    run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
