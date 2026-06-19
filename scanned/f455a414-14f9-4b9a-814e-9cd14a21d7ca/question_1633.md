# Q1633: High network limit off by one in encode

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses through a remote P2P peer sending crafted framed messages so `encode` in `network/src/protocols/discovery/protocol.rs` trigger a parser or protocol-state panic with a single malformed peer-controlled payload, violating malformed peer data must not crash a node or create low-cost network congestion, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `network/src/protocols/discovery/protocol.rs::encode`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: trigger a parser or protocol-state panic with a single malformed peer-controlled payload
- Invariant to test: malformed peer data must not crash a node or create low-cost network congestion
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
