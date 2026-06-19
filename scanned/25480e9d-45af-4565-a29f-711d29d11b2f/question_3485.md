# Q3485: High txpool boundary divergence in fmt

## Question
Can an unprivileged attacker enter through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions and use verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing to drive `fmt` in `tx-pool/src/component/pool_map.rs` across a boundary where pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating the invariant that pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/component/pool_map.rs::fmt`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
