# Q3410: High txpool state transition mismatch in update_proposals

## Question
Can an unprivileged attacker enter through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state and sequence verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing so `update_proposals` in `tx-pool/src/block_assembler/mod.rs` observes pre-state and post-state from different views, letting the flow make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/block_assembler/mod.rs::update_proposals`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
