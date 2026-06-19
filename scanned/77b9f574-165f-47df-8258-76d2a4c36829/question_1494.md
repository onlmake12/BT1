# Q1494: High network boundary divergence in from

## Question
Can an unprivileged attacker enter through a discovery peer advertising adversarial addresses and node records and use header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses to drive `from` in `network/src/network_group.rs` across a boundary where trigger a parser or protocol-state panic with a single malformed peer-controlled payload, violating the invariant that P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `network/src/network_group.rs::from`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: trigger a parser or protocol-state panic with a single malformed peer-controlled payload
- Invariant to test: P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
