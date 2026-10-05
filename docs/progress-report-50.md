# Optional hosted next-task hook

Platform PR #93 passed 922 tests and merged with Receipt rcpt_09418fe94c8f49a8b7d3c5b1345282eb. Platform PR #94 adds the hosted provenance fields and supporting links.

Core now adds explicit --hosted installation and hook execution using existing organisation read access. Raw prompts are not sent; response identities/counts/labels/Receipt IDs are validated and context is rebuilt from bounded structured data. Missing access and network deadlines fail open without claiming context delivery. Older local/OSN projections keep their shape.

Validation uses independent GitHub CI and Cloud Receipts because the local executor stopped responding. No unreturned local command is treated as a pass. Tests use fake HTTP transports and no provider APIs or real credentials.

This is descriptive verified-check advice, not proof of model consumption or recursive autonomous policy changes. Package publication and actual cloud export acceptance remain separate.
