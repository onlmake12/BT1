# Q1942: Critical network canonical encoding ambiguity in ShortIdsVerifier

## Question
Can an unprivileged attacker craft alternate encodings for header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses through a remote P2P peer sending crafted framed messages so `ShortIdsVerifier` in `sync/src/relayer/compact_block_verifier.rs` accepts two representations for one security object and make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse, violating sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `sync/src/relayer/compact_block_verifier.rs::ShortIdsVerifier`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
