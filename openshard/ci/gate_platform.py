"""The merge gate's one Platform read: the gate verdict for a commit.

``GET {api}/v1/cloud/github/gate-verdict?sha=<sha>`` with a GitHub Actions
OIDC token (audience ``openshard-gate``), or, for CI outside GitHub
Actions, ``GET {api}/v1/orgs/{org}/commits/{sha}/gate-verdict`` with an
organisation API key. Both return ``openshard.gate_verdict.v1``
(docs/merge-gate.md).

Every failure becomes a :class:`PlatformError` with a plain reason; nothing
here ever stands in for a verdict. ``poll`` waits while the Platform says
``pending`` (and through transient errors) until a final answer or the
deadline, and the deadline is itself an error. Never raises.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA = "openshard.gate_verdict.v1"
OIDC_AUDIENCE = "openshard-gate"
DEFAULT_API = "https://api.openshard.dev"
STATUSES = frozenset({"verified", "failed", "pending", "none"})
POLICIES = frozenset({"agent_prs", "all_prs"})

ERROR_UNREACHABLE = "unreachable"
ERROR_AUTH = "auth"
ERROR_FORK = "fork"
ERROR_NO_CREDENTIALS = "no_credentials"
ERROR_BAD_RESPONSE = "bad_response"
ERROR_TIMEOUT = "timeout"
ERROR_REJECTED = "rejected"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_MAX_MESSAGE = 200

FORK_MESSAGE = "Fork PRs can't be verified: GitHub gives them no credentials."
NO_CREDENTIALS_MESSAGE = (
    "No credentials for the Openshard Platform: give the job `permissions: id-token: write`, "
    "or pass the `api-key` and `org` inputs."
)


@dataclass(frozen=True)
class PlatformError:
    kind: str
    message: str
    transient: bool = False
    last: dict[str, Any] | None = None  # the last pending answer before a timeout, for the summary


@dataclass(frozen=True)
class Credentials:
    """How to authenticate: an OIDC request (GitHub Actions) or an API key with its organisation."""

    oidc_url: str | None = None
    oidc_request_token: str | None = None
    api_key: str | None = None
    org: str | None = None


def credentials_from_env(env: Mapping[str, str]) -> Credentials | PlatformError:
    """Credentials from the gate's environment, or the reason there are none.

    An API key needs its organisation. Without either credential, a pull
    request from a fork gets the fork reason: GitHub withholds both OIDC
    tokens and secrets from fork pull requests.
    """
    api_key = (env.get("OPENSHARD_GATE_API_KEY") or "").strip()
    org = (env.get("OPENSHARD_GATE_ORG") or "").strip()
    if api_key:
        if not _UUID_RE.match(org):
            return PlatformError(ERROR_NO_CREDENTIALS, "The `api-key` input needs the `org` input (the organisation id).")
        return Credentials(api_key=api_key, org=org)
    url = (env.get("ACTIONS_ID_TOKEN_REQUEST_URL") or "").strip()
    token = (env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN") or "").strip()
    if url and token:
        return Credentials(oidc_url=url, oidc_request_token=token)
    if (env.get("OPENSHARD_GATE_FORK") or "").strip().lower() == "true":
        return PlatformError(ERROR_FORK, FORK_MESSAGE)
    return PlatformError(ERROR_NO_CREDENTIALS, NO_CREDENTIALS_MESSAGE)


def _short_message(data: object) -> str | None:
    if isinstance(data, dict):
        err = data.get("error")
        msg = err.get("message") if isinstance(err, dict) else None
        if isinstance(msg, str) and msg.strip():
            return " ".join(msg.split())[:_MAX_MESSAGE]
    return None


def _json(response: Any) -> object:
    try:
        return response.json()
    except Exception:
        return None


def validate(data: object, sha: str) -> dict[str, Any] | PlatformError:
    """*data* when it is a gate verdict for *sha*, else why it is not.

    Under a merge queue the Platform answers for the pull request head and
    names the queue commit that was asked about in ``merge_group``; that
    answer is for *sha* only when ``merge_group.requested_sha`` is *sha* and
    ``merge_group.head_sha`` is the answer's ``sha``.
    """
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return PlatformError(ERROR_BAD_RESPONSE, "The Platform's answer is not an Openshard gate verdict.")
    answered = str(data.get("sha", "")).lower()
    group = data.get("merge_group")
    if group is not None and not (
        isinstance(group, dict) and is_sha(group.get("requested_sha")) and is_sha(group.get("head_sha"))
        and isinstance(group.get("pull_request"), int) and not isinstance(group.get("pull_request"), bool)
    ):
        return PlatformError(ERROR_BAD_RESPONSE, "The Platform's gate verdict has a malformed merge_group.")
    if group is None:
        if answered != sha:
            return PlatformError(ERROR_BAD_RESPONSE, "The Platform answered for a different commit.")
    elif group["requested_sha"] != sha or group["head_sha"] != answered:
        return PlatformError(ERROR_BAD_RESPONSE, "The Platform answered for a different merge queue commit.")
    if data.get("status") not in STATUSES:
        return PlatformError(ERROR_BAD_RESPONSE, "The Platform's gate verdict has an unknown status.")
    policy = data.get("policy")
    if not isinstance(policy, dict) or policy.get("require_receipt") not in POLICIES:
        return PlatformError(ERROR_BAD_RESPONSE, "The Platform's gate verdict has no team policy.")
    if not isinstance(data.get("receipts"), list):
        return PlatformError(ERROR_BAD_RESPONSE, "The Platform's gate verdict has malformed receipts.")
    if data.get("reason") is not None and not isinstance(data.get("reason"), str):
        return PlatformError(ERROR_BAD_RESPONSE, "The Platform's gate verdict has a malformed reason.")
    return data


def _incomplete(answer: dict[str, Any]) -> str:
    """What the pending Receipts say about their verification, from the fields the Platform sent."""
    states: list[str] = []
    for receipt in answer.get("receipts") or []:
        block = receipt.get("verification") if isinstance(receipt, dict) else None
        result = block.get("result") if isinstance(block, dict) else None
        if isinstance(result, str) and result and result not in states:
            states.append(" ".join(result.split())[:64])
    return ", ".join(states[:5]) or "unknown"


def fetch_once(api: str, sha: str, creds: Credentials, http: Any) -> dict[str, Any] | PlatformError:
    """One gate-verdict read. *http* is an ``httpx.Client`` (or anything with ``get``)."""
    base = api.rstrip("/")
    try:
        if creds.api_key:
            url = f"{base}/v1/orgs/{urllib.parse.quote(str(creds.org), safe='')}/commits/{sha}/gate-verdict"
            bearer = creds.api_key
        else:
            # The request URL already carries a query string; httpx's ``params``
            # would replace it, so the audience is appended as the workflow does.
            oidc_url = str(creds.oidc_url)
            minted = http.get(
                f"{oidc_url}{'&' if '?' in oidc_url else '?'}audience={OIDC_AUDIENCE}",
                headers={"Authorization": f"Bearer {creds.oidc_request_token}"},
            )
            value = _json(minted)
            raw = value.get("value") if minted.status_code == 200 and isinstance(value, dict) else None
            if not isinstance(raw, str) or not raw:
                return PlatformError(ERROR_AUTH, f"GitHub did not issue an OIDC token (HTTP {minted.status_code}).",
                                     transient=minted.status_code >= 500)
            bearer = raw
            url = f"{base}/v1/cloud/github/gate-verdict"
        response = http.get(url, params=None if creds.api_key else {"sha": sha},
                            headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"})
    except Exception as exc:
        return PlatformError(ERROR_UNREACHABLE, f"The Openshard Platform at {base} is unreachable ({type(exc).__name__}).",
                             transient=True)
    status = response.status_code
    data = _json(response)
    detail = _short_message(data)
    suffix = f": {detail}" if detail else "."
    if status in (401, 403):
        return PlatformError(ERROR_AUTH, f"The Openshard Platform refused this job's credentials (HTTP {status}){suffix}")
    if status == 429 or status >= 500:
        return PlatformError(ERROR_UNREACHABLE, f"The Openshard Platform is unavailable (HTTP {status}){suffix}",
                             transient=True)
    if status != 200:
        return PlatformError(ERROR_REJECTED, f"The Openshard Platform rejected the request (HTTP {status}){suffix}")
    return validate(data, sha)


def poll(
    fetch: Callable[[], dict[str, Any] | PlatformError],
    *,
    wait_seconds: float,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | PlatformError:
    """Fetch until the verdict is final, a permanent error, or *wait_seconds* pass."""
    deadline = monotonic() + max(0.0, wait_seconds)
    last: dict[str, Any] | None = None
    while True:
        result = fetch()
        if isinstance(result, dict) and result.get("status") == "pending":
            last = result
        waiting = (isinstance(result, PlatformError) and result.transient) or (
            isinstance(result, dict) and result.get("status") == "pending")
        if not waiting:
            return result
        remaining = deadline - monotonic()
        if remaining <= 0:
            minutes = f"{wait_seconds / 60:g}"
            if isinstance(result, PlatformError):
                return PlatformError(ERROR_UNREACHABLE, f"{result.message} Still failing after {minutes} min.",
                                     last=last)
            if result.get("receipts"):
                return PlatformError(ERROR_TIMEOUT, (
                    f"Verification incomplete (partial) after {minutes} min: the Receipt's verification is "
                    f"{_incomplete(result)}, not a verified pass or failure."), last=result)
            return PlatformError(ERROR_TIMEOUT, f"Verification did not finish in {minutes} min.", last=result)
        sleep(min(interval, remaining))


def is_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.match(value))


__all__ = [
    "DEFAULT_API",
    "FORK_MESSAGE",
    "OIDC_AUDIENCE",
    "SCHEMA",
    "Credentials",
    "PlatformError",
    "credentials_from_env",
    "fetch_once",
    "is_sha",
    "poll",
    "validate",
]
