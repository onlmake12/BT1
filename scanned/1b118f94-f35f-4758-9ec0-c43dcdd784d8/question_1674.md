# Q1674: Critical network boundary divergence in try_from

## Question
Can an unprivileged attacker enter through a remote P2P peer sending crafted framed messages and use compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs to drive `try_from` in `network/src/protocols/hole_punching/component/connection_request_delivered.rs` across a boundary where make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse, violating the invariant that peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/protocols/hole_punching/component/connection_request_delivered.rs::try_from`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
