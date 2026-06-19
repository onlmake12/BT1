# Q1499: High network parser precheck gap in is_outbound

## Question
Can an unprivileged attacker submit malformed-but-reachable compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs through a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks so `is_outbound` in `network/src/peer.rs` performs expensive or unsafe work before validation and make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse, violating P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `network/src/peer.rs::is_outbound`
- Entrypoint: a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks
- Attacker controls: compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
