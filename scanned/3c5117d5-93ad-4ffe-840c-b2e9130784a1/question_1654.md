# Q1654: Critical network boundary divergence in new

## Question
Can an unprivileged attacker enter through a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks and use compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs to drive `new` in `network/src/protocols/feeler.rs` across a boundary where cause high CPU or memory work before frame/message limits and peer punishment are applied, violating the invariant that sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/protocols/feeler.rs::new`
- Entrypoint: a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks
- Attacker controls: compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
