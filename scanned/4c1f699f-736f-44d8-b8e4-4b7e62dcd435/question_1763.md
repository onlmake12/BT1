# Q1763: High network canonical encoding ambiguity in max_frame_length

## Question
Can an unprivileged attacker craft alternate encodings for header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses through a transaction/block relayer sending repeated malformed-but-cheap payloads so `max_frame_length` in `network/src/protocols/support_protocols.rs` accepts two representations for one security object and make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse, violating malformed peer data must not crash a node or create low-cost network congestion, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `network/src/protocols/support_protocols.rs::max_frame_length`
- Entrypoint: a transaction/block relayer sending repeated malformed-but-cheap payloads
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: malformed peer data must not crash a node or create low-cost network congestion
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
