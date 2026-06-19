# Q1993: High network boundary divergence in new

## Question
Can an unprivileged attacker enter through a discovery peer advertising adversarial addresses and node records and use compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs to drive `new` in `sync/src/synchronizer/block_process.rs` across a boundary where desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply, violating the invariant that peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `sync/src/synchronizer/block_process.rs::new`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs
- Exploit idea: desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply
- Invariant to test: peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
