"""OpenCode plugin installation for OpenShard auto-capture (PR12).

Writes the OpenShard capture plugin to the repository's project-local
plugin directory, ``<repo>/.opencode/plugins/openshard.ts`` -- OpenCode's
supported plugin mechanism loads every JS/TS file there automatically, so
no ``opencode.json`` edit is needed and no other plugin or setting is
touched. The plugin source lives in ``PLUGIN_SOURCE`` below; the installer
renders it with the capture service port and compares content, so
re-running is a no-op unless the port or the plugin version changed.

Ownership
---------
The file starts with ``PLUGIN_MARKER``. Install only ever overwrites a
file carrying that marker (an older OpenShard plugin); a file at the same
path without it belongs to the user and is left alone
(``status="skipped_existing"``). Uninstall removes the file only when the
marker is present. When OpenShard creates the file it also adds it to the
repository's local ``.git/info/exclude``.

The plugin itself
-----------------
Plain TypeScript with no imports, no dependencies and no OpenShard
business logic: it observes ``session.created`` / ``session.idle`` /
``session.deleted`` / ``file.edited`` / ``message.updated`` events plus the
``chat.message`` and ``tool.execute.after`` hooks, reduces each to a small
JSON document (bounded strings, never tool output, never full messages)
and POSTs it to ``http://127.0.0.1:<port>/hooks/opencode``. If the service
is not reachable it keeps a small bounded in-memory buffer and asks
``openshard capture start`` to bring the service up -- at most once per
``START_COOLDOWN_MS`` (60 s) for the life of the OpenCode process, so a
service that dies mid-session is restarted on the next failed delivery
after the cooldown while a persistently missing OpenShard never causes a
spawn storm. Every successful delivery (and a short timer after each
start attempt) flushes the buffer in order. If OpenShard is not installed
at all the plugin fails silently. Translation to canonical Events happens
in the service (``adapters/opencode_plugin.py``), never in the plugin.
"""

from __future__ import annotations

import os
from pathlib import Path

from openshard.adapters.claude_capture_client import DEFAULT_PORT, OPENCODE_HOOK_PATH
from openshard.adapters.claude_hooks_install import (
    ClaudeHooksInstallResult,
    ensure_local_settings_ignored,
)

PLUGIN_VERSION = 2
PLUGIN_MARKER = "// openshard-capture-plugin"
PLUGIN_RELPATH = Path(".opencode") / "plugins" / "openshard.ts"
_MAX_PLUGIN_BYTES = 64 * 1024

# NOTE: keep this a template with exactly the placeholders substituted in
# render_plugin_source(); everything else is literal TypeScript.
PLUGIN_SOURCE = r'''__MARKER__ v__VERSION__ -- managed by `openshard setup`; edits are overwritten on reinstall.
// Sends bounded lifecycle facts about this OpenCode session to the local
// OpenShard capture service (127.0.0.1 only). Never sends message bodies,
// tool output, or file contents. Remove with `openshard capture uninstall opencode`.
const PORT = __PORT__
const PATH = "__HOOK_PATH__"
const MAX_TEXT = 400
const MAX_PENDING = 200
// A failed delivery may ask `openshard capture start` to (re)start the
// service, but never more often than this: bounded retry, no spawn storm.
const START_COOLDOWN_MS = 60000
const START_FLUSH_DELAY_MS = 1500

export const OpenShardCapture = async ({ directory, worktree, $ }: any) => {
  const url = "http://127.0.0.1:" + PORT + PATH
  const pending: string[] = []
  const children = new Set<string>()
  let lastStartAt = -Infinity
  let lastSession: string | null = null

  const clip = (v: any): string | undefined =>
    typeof v === "string" && v.length > 0 ? v.slice(0, MAX_TEXT) : undefined

  const startService = () => {
    const now = Date.now()
    if (now - lastStartAt < START_COOLDOWN_MS) return
    lastStartAt = now
    try {
      if (typeof $ === "function") {
        Promise.resolve($`openshard capture start`.quiet().nothrow())
          .catch(() => {})
          .finally(() => setTimeout(flush, START_FLUSH_DELAY_MS))
      }
    } catch {}
  }

  const post = async (body: string): Promise<boolean> => {
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body,
        signal: AbortSignal.timeout(1500),
      })
      return r.ok
    } catch {
      return false
    }
  }

  // Replays the buffer in order. Guarded so a timer flush and a
  // delivery-triggered flush never race (which would post the same
  // document twice); the running flush simply picks up anything appended.
  let flushing = false
  const flush = async () => {
    if (flushing) return
    flushing = true
    try {
      while (pending.length) {
        if (!(await post(pending[0]))) return
        pending.shift()
      }
    } finally {
      flushing = false
    }
  }

  const buffer = (body: string) => {
    if (pending.length < MAX_PENDING) pending.push(body)
  }

  const send = (doc: Record<string, any>) => {
    const body = JSON.stringify({ agent: "opencode", directory, worktree, ...doc })
    if (pending.length) {
      // Keep order: older buffered documents go first; this one joins the
      // queue and the flush delivers it in turn. Anything still pending
      // afterwards means the service is (still) down -> bounded restart.
      buffer(body)
      void flush().then(() => {
        if (pending.length) startService()
      })
      return
    }
    void post(body).then((ok) => {
      if (ok) return
      buffer(body)
      startService()
    })
  }

  const isChild = (id: any) => typeof id === "string" && children.has(id)

  return {
    event: async ({ event }: any) => {
      const p = event?.properties ?? {}
      switch (event?.type) {
        case "session.created": {
          const info = p.info ?? {}
          if (typeof info.parentID === "string" && info.parentID) {
            if (children.size < 1000) children.add(info.id)
            return
          }
          send({ event: "session.created", session_id: info.id, parent_id: null })
          return
        }
        case "session.idle":
          if (isChild(p.sessionID)) return
          send({ event: "session.idle", session_id: p.sessionID })
          return
        case "session.deleted": {
          const info = p.info ?? {}
          if (isChild(info.id)) return
          send({ event: "session.deleted", session_id: info.id })
          return
        }
        case "file.edited":
          if (lastSession) send({ event: "file.edited", session_id: lastSession, file_path: clip(p.file) })
          return
        case "message.updated": {
          const info = p.info ?? {}
          if (info.role !== "assistant" || !info.time?.completed || isChild(info.sessionID)) return
          send({
            event: "message.updated",
            session_id: info.sessionID,
            message_id: info.id,
            provider_id: info.providerID,
            model_id: info.modelID,
            cost: typeof info.cost === "number" ? info.cost : null,
            tokens: info.tokens ?? null,
          })
          return
        }
      }
    },
    "chat.message": async (input: any, output: any) => {
      if (isChild(input?.sessionID)) return
      lastSession = input?.sessionID ?? lastSession
      const model = input?.model ?? output?.message?.model ?? {}
      const first = (output?.parts ?? []).find((x: any) => x?.type === "text" && typeof x.text === "string")
      send({
        event: "chat.message",
        session_id: input?.sessionID,
        prompt: clip(first?.text),
        provider_id: model.providerID,
        model_id: model.modelID,
      })
    },
    "tool.execute.after": async (input: any, _output: any) => {
      if (isChild(input?.sessionID)) return
      lastSession = input?.sessionID ?? lastSession
      const args = input?.args ?? {}
      send({
        event: "tool.execute.after",
        session_id: input?.sessionID,
        tool: clip(input?.tool),
        file_path: clip(args.filePath),
        command: clip(args.command),
      })
    },
  }
}
'''


def render_plugin_source(port: int = DEFAULT_PORT) -> str:
    return (
        PLUGIN_SOURCE.replace("__MARKER__", PLUGIN_MARKER)
        .replace("__VERSION__", str(PLUGIN_VERSION))
        .replace("__PORT__", str(int(port)))
        .replace("__HOOK_PATH__", OPENCODE_HOOK_PATH)
    )


def is_openshard_plugin(text: object) -> bool:
    return isinstance(text, str) and text.lstrip().startswith(PLUGIN_MARKER)


def plugin_path(repo_root: Path) -> Path:
    return Path(repo_root) / PLUGIN_RELPATH


def _read_plugin(path: Path) -> tuple[str | None, str | None]:
    """``(text, error)``; a missing file is ``("", None)``."""
    if not path.exists():
        return "", None
    try:
        if path.stat().st_size > _MAX_PLUGIN_BYTES:
            return None, f"{path} is unexpectedly large; OpenShard will not modify it."
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"Could not read {path.name}: {type(exc).__name__}"


def installed_plugin_port(text: object) -> int | None:
    """Port an installed OpenShard plugin targets, or None."""
    if not is_openshard_plugin(text):
        return None
    for line in str(text).splitlines():
        stripped = line.strip()
        if stripped.startswith("const PORT ="):
            value = stripped.split("=", 1)[1].strip()
            if value.isdigit() and 0 < int(value) < 65536:
                return int(value)
    return None


def installed_plugin_version(text: object) -> int | None:
    if not is_openshard_plugin(text):
        return None
    head = str(text).lstrip().split("\n", 1)[0]
    token = head[len(PLUGIN_MARKER):].strip().split(" ", 1)[0]
    if token.startswith("v") and token[1:].isdigit():
        return int(token[1:])
    return None


def detect_plugin(repo_root: Path) -> dict:
    """Read-only state of the plugin file: ``{"state", "port", "version", "error"}``.

    ``state`` is ``"openshard"`` / ``"custom"`` / ``"absent"``.
    """
    text, err = _read_plugin(plugin_path(repo_root))
    if err or text is None:
        return {"state": "custom", "port": None, "version": None, "error": err}
    if not text:
        return {"state": "absent", "port": None, "version": None, "error": None}
    if is_openshard_plugin(text):
        return {"state": "openshard", "port": installed_plugin_port(text),
                "version": installed_plugin_version(text), "error": None}
    return {"state": "custom", "port": None, "version": None, "error": None}


def _write_plugin(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _error(message: str, path: Path | None = None) -> ClaudeHooksInstallResult:
    return ClaudeHooksInstallResult(status="error", settings_path=path, message=message)


def install_opencode_plugin(*, repo_root: Path, port: int | None = None) -> ClaudeHooksInstallResult:
    """Write the OpenShard plugin into ``<repo_root>/.opencode/plugins/``. Never raises."""
    try:
        root = Path(repo_root)
        path = plugin_path(root)
        if port is None:
            from openshard.adapters.claude_capture_client import resolve_port

            port = resolve_port()
        desired = render_plugin_source(port)
        existing, err = _read_plugin(path)
        if err or existing is None:
            return _error(err or "Could not read the OpenCode plugin file.", path)
        if existing and not is_openshard_plugin(existing):
            return ClaudeHooksInstallResult(
                status="skipped_existing", settings_path=path,
                message=(
                    f"{PLUGIN_RELPATH.as_posix()} already exists and is not OpenShard's plugin; "
                    "OpenShard will not replace it. Move it aside and re-run to enable OpenCode capture."
                ),
            )
        if existing == desired:
            return ClaudeHooksInstallResult(
                status="already_installed", settings_path=path,
                message="OpenCode capture plugin already installed for this repository.",
            )
        created = not existing
        _write_plugin(path, desired)
        warnings: list[str] = []
        if created:
            ignore_warning = ensure_local_settings_ignored(
                root, PLUGIN_RELPATH.as_posix(), note="added by openshard capture install opencode",
            )
            if ignore_warning:
                warnings.append(ignore_warning)
        return ClaudeHooksInstallResult(
            status="installed" if created else "updated", settings_path=path,
            message="OpenCode capture plugin installed." if created else "OpenCode capture plugin updated.",
            warnings=warnings,
        )
    except Exception as exc:
        return _error(f"Failed to install the OpenCode plugin: {type(exc).__name__}")


def uninstall_opencode_plugin(*, repo_root: Path) -> ClaudeHooksInstallResult:
    """Remove the OpenShard plugin file -- only if it is OpenShard's. Never raises."""
    try:
        path = plugin_path(Path(repo_root))
        existing, err = _read_plugin(path)
        if err or existing is None:
            return _error(err or "Could not read the OpenCode plugin file.", path)
        if not existing:
            return ClaudeHooksInstallResult(
                status="not_installed", settings_path=path, message="No OpenShard OpenCode plugin was installed.",
            )
        if not is_openshard_plugin(existing):
            return ClaudeHooksInstallResult(
                status="not_installed", settings_path=path,
                message=f"{PLUGIN_RELPATH.as_posix()} is not OpenShard's plugin; nothing removed.",
            )
        path.unlink()
        try:
            # Drop now-empty plugin directories OpenShard itself created; a
            # directory with anything else in it is left alone.
            path.parent.rmdir()
            path.parent.parent.rmdir()
        except OSError:
            pass
        return ClaudeHooksInstallResult(
            status="removed", settings_path=path, message="OpenCode capture plugin removed.",
        )
    except Exception as exc:
        return _error(f"Failed to remove the OpenCode plugin: {type(exc).__name__}")
