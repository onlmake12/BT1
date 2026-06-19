# Q1832: Critical network resource amplification in new

## Question
Can an unprivileged attacker repeatedly send small message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths through a discovery peer advertising adversarial addresses and node records to make `new` in `network/src/services/protocol_type_checker.rs` amplify CPU, memory, storage, or bandwidth and trigger a parser or protocol-state panic with a single malformed peer-controlled payload, violating malformed peer data must not crash a node or create low-cost network congestion, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/services/protocol_type_checker.rs::new`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths
- Exploit idea: trigger a parser or protocol-state panic with a single malformed peer-controlled payload
- Invariant to test: malformed peer data must not crash a node or create low-cost network congestion
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
