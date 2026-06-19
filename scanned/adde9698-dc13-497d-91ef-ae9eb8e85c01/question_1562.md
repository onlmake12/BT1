# Q1562: Critical network restart reorg persistence in is_ok

## Question
Can an unprivileged attacker shape header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses through a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks, then force normal restart, reorg, retry, or replay handling so `is_ok` in `network/src/peer_store/mod.rs` persists inconsistent state and trigger a parser or protocol-state panic with a single malformed peer-controlled payload, violating P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/peer_store/mod.rs::is_ok`
- Entrypoint: a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: trigger a parser or protocol-state panic with a single malformed peer-controlled payload
- Invariant to test: P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
