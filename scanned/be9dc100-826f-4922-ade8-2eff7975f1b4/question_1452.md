# Q1452: Critical network limit off by one in new

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing through a remote P2P peer sending crafted framed messages so `new` in `network/src/compress.rs` cause high CPU or memory work before frame/message limits and peer punishment are applied, violating malformed peer data must not crash a node or create low-cost network congestion, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/compress.rs::new`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: malformed peer data must not crash a node or create low-cost network congestion
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
