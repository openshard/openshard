# relay

A tiny local job queue. Jobs live in a plain tab-separated text file so the
queue can be inspected, diffed and edited with ordinary tools.

```text
$ python -m relay --queue relay.queue add build "make -j4" --retries 2
$ python -m relay --queue relay.queue add deploy "./deploy.sh"
$ python -m relay --queue relay.queue list
build   make -j4        2
deploy  ./deploy.sh     0
```

* No runtime dependencies (standard library only, Python 3.11+).
* Queue files are read by older releases of `relay`, so the record format
  is versioned and backwards compatible. See `CONTRIBUTING.md` before
  changing anything about job records.

## Commands

| Command | What it does |
|---|---|
| `add NAME COMMAND [options]` | Append a job. Names must be unique. |
| `list` | Print every job, one per line. |
| `show NAME` | Print one job as `field: value` lines. |
| `remove NAME` | Delete a job. |

`--queue PATH` (or `RELAY_QUEUE`) selects the queue file; default `relay.queue`.

## Development

```text
python -m unittest discover -s tests -v
```
