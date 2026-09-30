"""Remote capture: evidence leaves an ephemeral agent runtime while the agent works.

A cloud coding task runs in a VM or container that is destroyed when the
task ends, and sometimes before. Everything OpenShard records locally
(``.openshard/runs.jsonl``, the Receipt) dies with it. Remote capture streams
the Events the hook adapters already build to a hosted *remote capture* on
the Platform, in small batches, from the first hook on:

    hook event -> Event (unchanged) -> local spool (append first)
               -> batch POST with the capture's short-lived token
               -> acknowledged only once the Platform accepted it

so whatever left the runtime before it died is kept, and the Platform can
say truthfully how the session ended: with its own end Event, or without one.

Modules:

``config``     the attachment: which capture, where, with which token
``spool``      the durable local queue and its acknowledgement state
``transport``  the HTTPS calls, classified
``collector``  the hook-path tap, the flush, and the background trigger

Nothing here runs an agent, owns a model, or gates anything. It is evidence
transport, and it never blocks a hook on the network.
"""
