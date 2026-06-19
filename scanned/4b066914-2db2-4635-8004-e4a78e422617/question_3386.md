# Q3386: High txpool replay reorder race in partition_nonce

## Question
Can an unprivileged attacker replay, reorder, or delay block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state so `partition_nonce` in `miner/src/worker/mod.rs` takes a stale branch and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, breaking the invariant that pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `miner/src/worker/mod.rs::partition_nonce`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
