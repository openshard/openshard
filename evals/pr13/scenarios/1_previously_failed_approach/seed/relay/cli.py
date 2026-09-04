"""Command line for relay: ``python -m relay [--queue PATH] COMMAND ...``."""

from __future__ import annotations

import argparse
import os
import sys

from relay._schema import FIELDS
from relay.queue import QueueError, QueueFile
from relay.records import Job, RecordError

DEFAULT_QUEUE = "relay.queue"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relay", description="A tiny local job queue.")
    parser.add_argument(
        "--queue",
        default=os.environ.get("RELAY_QUEUE", DEFAULT_QUEUE),
        help=f"queue file (default: $RELAY_QUEUE or {DEFAULT_QUEUE})",
    )
    # "subcommand", not "command": the add subparser's positional is the
    # job's *command* field and must not clobber the dispatch attribute.
    commands = parser.add_subparsers(dest="subcommand", required=True)

    add = commands.add_parser("add", help="append a job to the queue")
    for field in FIELDS:
        if field.required:
            add.add_argument(field.name, help=field.help)
        else:
            add.add_argument(
                f"--{field.name}", type=field.kind, default=field.default,
                help=f"{field.help} (default: {field.default})",
            )

    commands.add_parser("list", help="print every job, one per line")

    show = commands.add_parser("show", help="print one job as field: value lines")
    show.add_argument("name")

    remove = commands.add_parser("remove", help="delete a job")
    remove.add_argument("name")
    return parser


def format_row(job: Job) -> str:
    return "\t".join(str(value) for value in job.to_dict().values())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    queue = QueueFile(args.queue)
    try:
        if args.subcommand == "add":
            values = {field.name: getattr(args, field.name) for field in FIELDS}
            queue.add(Job(**values))
        elif args.subcommand == "list":
            for job in queue.ordered():
                print(format_row(job))
        elif args.subcommand == "show":
            job = queue.find(args.name)
            if job is None:
                raise QueueError(f"no job named {args.name!r}")
            for key, value in job.to_dict().items():
                print(f"{key}: {value}")
        elif args.subcommand == "remove":
            queue.remove(args.name)
    except (QueueError, RecordError) as exc:
        print(f"relay: {exc}", file=sys.stderr)
        return 1
    return 0
