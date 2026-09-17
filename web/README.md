# OpenShard dashboard (v0.5.0)

Hosted visibility for OpenShard receipts:

```text
use your coding agent normally
        ↓
OpenShard captures the work
        ↓
the receipt syncs
        ↓
you open this dashboard and see what agents have done
```

Three pages, nothing else:

| Route                   | Page            | What it answers                                             |
| ----------------------- | --------------- | ----------------------------------------------------------- |
| `/`                     | Recent work     | What ran, with which agent, did it complete, how long ago    |
| `/tasks/:taskId`        | Task            | Repo, agent, model, every attempt, the latest receipt summary |
| `/receipts/:receiptId`  | Receipt         | The full record: changes, checks, cost, evidence, integrity  |

## Run it

```bash
cd web
npm install
npm run dev        # fixture data, http://localhost:5173
npm run check      # typecheck + tests + production build
```

The app runs on fixture data by default. Point it at a sync service with:

```bash
VITE_OPENSHARD_API_URL=https://api.example.com npm run dev
```

## How it is put together

- `src/api/types.ts` mirrors `receipt_to_dict(extended=True)` from
  `openshard/history/views.py`, plus `task_id` and `synced_at`, which the
  hosted side adds. Nothing that the local privacy boundary omits (prompts,
  transcripts, diffs, absolute paths) is in the contract.
- `src/api/client.ts` is the whole backend seam: `listTasks`, `getTask`,
  `getReceipt`. `fixtureClient.ts` and `httpClient.ts` both implement it;
  `createApi()` picks one from the environment. Pages never import either.
- `src/lib/format.ts` copies the CLI's wording so the dashboard and
  `openshard last` say the same thing: costs are always `est.`, missing
  data is `not recorded`, integrity is `Matches (content hash)` and never
  "signed".
- `src/styles.css` takes its palette from `openshard/tui/styles.tcss`.

Deliberately not here yet: policy UI, analytics, graphs, OSN, managed
compute, settings.
