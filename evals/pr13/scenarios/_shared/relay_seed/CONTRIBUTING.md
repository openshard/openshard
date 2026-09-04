# Contributing to relay

## Layout

| Path | Role |
|---|---|
| `schema/jobs.json` | **Source of truth** for the fields of a job record. |
| `scripts/gen_schema.py` | Generates `relay/_schema.py` from `schema/jobs.json`. |
| `relay/_schema.py` | Generated. Do not edit by hand; regenerate instead. |
| `relay/records.py` | `Job`, plus the tab-separated line format. |
| `relay/queue.py` | `QueueFile`: load/save/add/remove/ordering. |
| `relay/cli.py` | The `python -m relay` command line. |

## Changing job records

1. Edit `schema/jobs.json`.
2. Run `python scripts/gen_schema.py` to regenerate `relay/_schema.py`.
3. Add or update tests under `tests/`.

CI runs `python scripts/gen_schema.py --check` and fails when the generated
module is out of date with its source, so a hand-edited `relay/_schema.py`
never gets merged.

## Compatibility rules for the queue file format

Queue files are shared between machines that may run different releases.

* Never remove, rename or reorder existing fields.
* New fields are optional, have a default, and go at the end of the schema.
* A record whose optional fields all hold their defaults must serialise
  exactly as it did before the field existed (trailing default columns are
  omitted), so files written by a new release still load in an old one.
* Lines with fewer columns than the schema (written by an older release)
  must load, with the missing fields taking their defaults.

## Checks

```text
python scripts/gen_schema.py --check
python -m unittest discover -s tests -v
```
