# Q1843: Critical network parser precheck gap in handle_watch_new_block

## Question
Can an unprivileged attacker submit malformed-but-reachable peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing through a discovery peer advertising adversarial addresses and node records so `handle_watch_new_block` in `notify/src/lib.rs` performs expensive or unsafe work before validation and trigger a parser or protocol-state panic with a single malformed peer-controlled payload, violating sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `notify/src/lib.rs::handle_watch_new_block`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing
- Exploit idea: trigger a parser or protocol-state panic with a single malformed peer-controlled payload
- Invariant to test: sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
