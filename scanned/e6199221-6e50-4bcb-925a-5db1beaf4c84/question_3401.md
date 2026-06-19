# Q3401: Critical txpool batch interaction bug in build_cellbase

## Question
Can an unprivileged attacker batch verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a peer relaying transactions that race recent-reject, orphan, and verification-queue state so `build_cellbase` in `tx-pool/src/block_assembler/mod.rs` handles the first item safely but applies incorrect assumptions to later items and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/block_assembler/mod.rs::build_cellbase`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
