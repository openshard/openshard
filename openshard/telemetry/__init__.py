"""Privacy-safe product telemetry (0.4.2).

One public entry point, :func:`openshard.telemetry.client.emit`, which never
raises, never blocks, never prints, and sends nothing unless the user has
seen the notice and left "Help improve OpenShard" on. What can be sent is
a closed, versioned schema of counts, versions, timings and enum values
(``schema.py``); no free text exists in it, so no code, prompt, path,
repository name, secret or identity can be carried by construction.

See ``docs/telemetry.md`` for the user-facing contract.
"""

from openshard.telemetry.client import emit

__all__ = ["emit"]
