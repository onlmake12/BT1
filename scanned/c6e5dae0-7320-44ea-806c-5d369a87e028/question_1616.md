# Q1616: Critical network cross module inconsistency in add_new_addr

## Question
Can an unprivileged attacker use a remote P2P peer sending crafted framed messages to make `add_new_addr` in `network/src/protocols/discovery/mod.rs` return a result that downstream modules interpret differently, where cause high CPU or memory work before frame/message limits and peer punishment are applied, violating sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/protocols/discovery/mod.rs::add_new_addr`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
