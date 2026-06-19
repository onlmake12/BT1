# Q1585: Critical network boundary divergence in remove_disconnected_peer

## Question
Can an unprivileged attacker enter through a discovery peer advertising adversarial addresses and node records and use header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses to drive `remove_disconnected_peer` in `network/src/peer_store/peer_store_impl.rs` across a boundary where make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse, violating the invariant that malformed peer data must not crash a node or create low-cost network congestion, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/peer_store/peer_store_impl.rs::remove_disconnected_peer`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: malformed peer data must not crash a node or create low-cost network congestion
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
