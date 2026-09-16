"""The user-global OpenShard home directory (``~/.openshard`` by default).

One resolver for every user-global file: the capture service's state,
log, start lock and token, telemetry state and queue, and the model /
provider caches. ``OPENSHARD_HOME`` overrides the location for all of them
at once (the test suite points it at a throw-away directory).

Repository-local state (``<repo>/.openshard/runs.jsonl`` and friends) is a
different thing and is never resolved through here.
"""

from __future__ import annotations

import os

HOME_ENV = "OPENSHARD_HOME"
_DEFAULT_DIRNAME = ".openshard"


def openshard_home(env: dict | os._Environ | None = None) -> str:
    """Return the OpenShard home directory as a string.

    ``OPENSHARD_HOME`` (non-blank) wins; otherwise ``~/.openshard``. Read on
    every call so a caller that passes its own *env* mapping sees exactly
    that mapping.
    """
    env = os.environ if env is None else env
    override = env.get(HOME_ENV)
    if isinstance(override, str) and override.strip():
        return override.strip()
    return os.path.join(os.path.expanduser("~"), _DEFAULT_DIRNAME)
