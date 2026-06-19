# Q1695: Critical network boundary divergence in try_nat_traversal

## Question
Can an unprivileged attacker enter through a remote P2P peer sending crafted framed messages and use header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses to drive `try_nat_traversal` in `network/src/protocols/hole_punching/component/mod.rs` across a boundary where make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse, violating the invariant that P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/protocols/hole_punching/component/mod.rs::try_nat_traversal`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
