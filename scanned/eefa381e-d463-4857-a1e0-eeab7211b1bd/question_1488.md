# Q1488: Critical network replay reorder race in From

## Question
Can an unprivileged attacker replay, reorder, or delay header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses through a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks so `From` in `network/src/network_group.rs` takes a stale branch and desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply, breaking the invariant that malformed peer data must not crash a node or create low-cost network congestion, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/network_group.rs::From`
- Entrypoint: a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply
- Invariant to test: malformed peer data must not crash a node or create low-cost network congestion
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
