"""Learning Loop V1: evidence-backed lessons from prior Shards.

    runs.jsonl -> observations -> learning signals -> relevance -> OSN context
                                                   -> Receipt ``learning`` block
                                                   -> later outcomes (impact)

Signals are derived at read time from recorded Receipts and are never written
back into them, the same way ``routing.adaptive.outcome`` derives outcomes: a
sealed Receipt stays untouched, every existing Receipt can contribute without
migration, and a signal is always reproducible from the evidence behind it.

Signals are advisory. They are observations with sample sizes, never
rankings, never policy, and never an instruction to the model. The only
execution decision history can affect is the model Adaptive Routing V2
selects, and only through its existing, sample-gated history evidence (see
``openshard.learning.routing``).
"""
