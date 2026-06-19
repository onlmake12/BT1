# Q1842: Critical network limit off by one in handle_register_proposed_transaction

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths through a remote P2P peer sending crafted framed messages so `handle_register_proposed_transaction` in `notify/src/lib.rs` trigger a parser or protocol-state panic with a single malformed peer-controlled payload, violating malformed peer data must not crash a node or create low-cost network congestion, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `notify/src/lib.rs::handle_register_proposed_transaction`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths
- Exploit idea: trigger a parser or protocol-state panic with a single malformed peer-controlled payload
- Invariant to test: malformed peer data must not crash a node or create low-cost network congestion
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
