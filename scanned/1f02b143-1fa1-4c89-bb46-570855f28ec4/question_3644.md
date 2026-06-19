# Q3644: Critical txpool boundary divergence in estimate_fee_rate

## Question
Can an unprivileged attacker enter through a local miner process selecting proposals and uncles near limit boundaries and use verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing to drive `estimate_fee_rate` in `util/fee-estimator/src/estimator/mod.rs` across a boundary where force quadratic graph or selection behavior with few low-cost transactions, violating the invariant that valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fee-estimator/src/estimator/mod.rs::estimate_fee_rate`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
