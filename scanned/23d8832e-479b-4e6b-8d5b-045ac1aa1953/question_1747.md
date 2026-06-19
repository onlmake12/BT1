# Q1747: High network canonical encoding ambiguity in build_pong

## Question
Can an unprivileged attacker craft alternate encodings for message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths through a discovery peer advertising adversarial addresses and node records so `build_pong` in `network/src/protocols/ping.rs` accepts two representations for one security object and cause high CPU or memory work before frame/message limits and peer punishment are applied, violating malformed peer data must not crash a node or create low-cost network congestion, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `network/src/protocols/ping.rs::build_pong`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: malformed peer data must not crash a node or create low-cost network congestion
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
