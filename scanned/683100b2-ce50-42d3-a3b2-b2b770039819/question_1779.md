# Q1779: Critical network boundary divergence in new

## Question
Can an unprivileged attacker enter through a remote P2P peer sending crafted framed messages and use compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs to drive `new` in `network/src/services/dns_seeding/mod.rs` across a boundary where cause high CPU or memory work before frame/message limits and peer punishment are applied, violating the invariant that malformed peer data must not crash a node or create low-cost network congestion, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/services/dns_seeding/mod.rs::new`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: malformed peer data must not crash a node or create low-cost network congestion
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
