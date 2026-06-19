# Q1611: Critical network restart reorg persistence in is_disconnect

## Question
Can an unprivileged attacker shape compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs through a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks, then force normal restart, reorg, retry, or replay handling so `is_disconnect` in `network/src/protocols/discovery/addr.rs` persists inconsistent state and trigger a parser or protocol-state panic with a single malformed peer-controlled payload, violating peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/protocols/discovery/addr.rs::is_disconnect`
- Entrypoint: a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks
- Attacker controls: compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs
- Exploit idea: trigger a parser or protocol-state panic with a single malformed peer-controlled payload
- Invariant to test: peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
