# Q1628: Critical network restart reorg persistence in Display

## Question
Can an unprivileged attacker shape header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses through a discovery peer advertising adversarial addresses and node records, then force normal restart, reorg, retry, or replay handling so `Display` in `network/src/protocols/discovery/protocol.rs` persists inconsistent state and desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply, violating malformed peer data must not crash a node or create low-cost network congestion, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/protocols/discovery/protocol.rs::Display`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply
- Invariant to test: malformed peer data must not crash a node or create low-cost network congestion
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
