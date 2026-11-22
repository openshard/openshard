"""Guards: checks a team adds after correcting an agent, run on every later change.

A guard is one small file in ``.openshard/guards/<id>.yml``, committed with
the repository and changed only like any other code (by pull request). It
names a check command and the paths it protects. When a later commit touches
those paths, ``openshard verify --commit`` and the merge gate run the guard
alongside the change's own checks, whichever agent (or person) wrote it.

The guard's verdict is the exit code OpenShard observes, never a report from
the agent. A guard whose own file changed in the commit being verified is not
run unattended (``needs_approval``): an agent cannot rewrite a guard and have
it pass itself.

Kinds:

* ``test`` -- a command (usually a test) that must exit 0.
* ``note`` -- one line of advice for agents. Never run, never blocks.
"""

from openshard.guards.model import (
    GUARDS_DIR,
    Guard,
    GuardError,
    GuardSet,
    load_guards,
)
from openshard.guards.select import guards_for_change, path_matches

__all__ = [
    "GUARDS_DIR",
    "Guard",
    "GuardError",
    "GuardSet",
    "guards_for_change",
    "load_guards",
    "path_matches",
]
