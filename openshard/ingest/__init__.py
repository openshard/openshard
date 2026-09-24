"""Historical Ingestion v1 (docs/architecture/historical-ingestion.md).

Rebuilds honestly labelled receipts from coding-agent history that already
exists on disk, without ever claiming OpenShard observed that work live::

    SourceConnector -> Parser -> normalize (scrub boundary) -> enrich (git)
        -> dedupe -> ShardBuilder -> ReceiptBuilder, driven by a resumable job.

Public API: :func:`run_job`, :func:`resume_job`, :func:`cancel_job`,
:func:`scan`, :func:`discover_sources`, :func:`job_status`, :func:`list_jobs`.
"""

from openshard.ingest.jobs import (
    JobResult,
    JobSpec,
    cancel_job,
    discover_sources,
    job_status,
    list_jobs,
    resume_job,
    run_job,
    scan,
)

__all__ = [
    "JobResult",
    "JobSpec",
    "cancel_job",
    "discover_sources",
    "job_status",
    "list_jobs",
    "resume_job",
    "run_job",
    "scan",
]
