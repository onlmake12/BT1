# Q3409: High txpool batch interaction bug in update_blank

## Question
Can an unprivileged attacker batch duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions through a peer relaying transactions that race recent-reject, orphan, and verification-queue state so `update_blank` in `tx-pool/src/block_assembler/mod.rs` handles the first item safely but applies incorrect assumptions to later items and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/block_assembler/mod.rs::update_blank`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
