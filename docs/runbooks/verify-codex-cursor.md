# Runbook: a Verified Receipt from Codex or Cursor (~10 minutes)

Use a git repository with a small, fast test suite (pytest installed in its
`.venv`, or a `test` script in `package.json`) and a clean working tree.

1. **Install and connect** (once): `pipx install openshard` (or `pipx upgrade openshard`), then
   `openshard login` and approve this computer at app.openshard.dev.
2. **Set up capture**, in the repository: `openshard setup`, then `openshard doctor`.
   Codex or Cursor should be listed as configured.
   - **Codex:** open `codex` in the repository once and trust the project.
     Codex does not capture this first session. Then quit and reopen Codex.
     If Codex asks you to review hooks (`/hooks`), approve the `openshard hooks codex` entries.
   - **Cursor:** open the repository in the Cursor app and trust the workspace. Use the Agent chat.
3. **Run one small task** and ask the agent to commit it, for example:
   *"Make the failing test in tests/test_x.py pass, run the tests, then git commit."*
   The agent must run `git commit` itself. A commit you make by hand is not counted as the session's work.
4. **End the session:** quit Codex (`/quit`), or close the Cursor agent chat.
5. **Check locally** (wait about 30 seconds for the re-run to finish):
   ```sh
   openshard last                                   # "Verified  Passed (Openshard re-ran ...)"
   openshard verdict --commit "$(git rev-parse HEAD)" --json; echo "exit=$?"   # exit=0 means verified
   ```
6. **Send it to the Platform:** `openshard sync now`, then open app.openshard.dev and
   find the Receipt by its task text. It should show Codex or Cursor (external) and Verified.

## If it did not work

| What you see | What it means | Try |
|---|---|---|
| `openshard last` shows no new Receipt | the hooks did not fire | `openshard doctor`; Codex: is the project trusted? |
| Receipt exists, `verdict` exit 2, Verified "not checked" | no commit was linked to the session, or the re-run is still going | wait a minute, then run `openshard verify --receipt <id> --commit <sha>` yourself |
| `verdict` exit 1 | Openshard re-ran the tests and they failed on that commit | correct result, not a bug |
| Re-run says "unknown" / setup could not run | the test runner is not installed where Openshard looks (`.venv`) | install pytest into the repo's `.venv` |
| Local shows Verified, the app does not | sync has not sent it yet | `openshard sync status`, then `openshard sync now` again |

**Send back:** the output of `openshard doctor --json`, `openshard last --json`,
`openshard verdict --commit <sha> --json`, `openshard sync status --json`, the agent and
its version (`codex --version`, or Cursor → About), and the Receipt link from the app if it appeared.
