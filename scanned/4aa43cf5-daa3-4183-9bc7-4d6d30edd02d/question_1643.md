# Q1643: High network parser precheck gap in to_inner

## Question
Can an unprivileged attacker submit malformed-but-reachable compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs through a discovery peer advertising adversarial addresses and node records so `to_inner` in `network/src/protocols/discovery/state.rs` performs expensive or unsafe work before validation and desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply, violating malformed peer data must not crash a node or create low-cost network congestion, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `network/src/protocols/discovery/state.rs::to_inner`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs
- Exploit idea: desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply
- Invariant to test: malformed peer data must not crash a node or create low-cost network congestion
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
