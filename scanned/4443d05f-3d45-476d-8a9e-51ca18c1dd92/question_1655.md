# Q1655: Critical network boundary divergence in new

## Question
Can an unprivileged attacker enter through a remote P2P peer sending crafted framed messages and use message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths to drive `new` in `network/src/protocols/feeler.rs` across a boundary where make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse, violating the invariant that P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/protocols/feeler.rs::new`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
