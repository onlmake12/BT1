# Q3369: High txpool restart reorg persistence in run

## Question
Can an unprivileged attacker shape block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a peer relaying transactions that race recent-reject, orphan, and verification-queue state, then force normal restart, reorg, retry, or replay handling so `run` in `miner/src/worker/dummy.rs` persists inconsistent state and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `miner/src/worker/dummy.rs::run`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
