# Q1783: Critical network restart reorg persistence in seeding

## Question
Can an unprivileged attacker shape header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses through a remote P2P peer sending crafted framed messages, then force normal restart, reorg, retry, or replay handling so `seeding` in `network/src/services/dns_seeding/mod.rs` persists inconsistent state and make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse, violating malformed peer data must not crash a node or create low-cost network congestion, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/services/dns_seeding/mod.rs::seeding`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: malformed peer data must not crash a node or create low-cost network congestion
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
