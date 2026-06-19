# Q1780: Critical network cross module inconsistency in new

## Question
Can an unprivileged attacker use a remote P2P peer sending crafted framed messages to make `new` in `network/src/services/dns_seeding/mod.rs` return a result that downstream modules interpret differently, where make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse, violating sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/services/dns_seeding/mod.rs::new`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
