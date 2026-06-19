# Q1764: High network parser precheck gap in max_frame_length

## Question
Can an unprivileged attacker submit malformed-but-reachable message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths through a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks so `max_frame_length` in `network/src/protocols/support_protocols.rs` performs expensive or unsafe work before validation and cause high CPU or memory work before frame/message limits and peer punishment are applied, violating sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `network/src/protocols/support_protocols.rs::max_frame_length`
- Entrypoint: a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks
- Attacker controls: message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
