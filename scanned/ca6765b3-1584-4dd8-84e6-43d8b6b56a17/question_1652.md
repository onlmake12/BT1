# Q1652: Critical network restart reorg persistence in new

## Question
Can an unprivileged attacker shape message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths through a discovery peer advertising adversarial addresses and node records, then force normal restart, reorg, retry, or replay handling so `new` in `network/src/protocols/feeler.rs` persists inconsistent state and cause high CPU or memory work before frame/message limits and peer punishment are applied, violating P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/protocols/feeler.rs::new`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
