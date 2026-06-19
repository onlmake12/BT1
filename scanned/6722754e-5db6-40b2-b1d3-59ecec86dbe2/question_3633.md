# Q3633: Critical txpool differential path split in bucket_index_by_fee_rate

## Question
Can an unprivileged attacker reach `bucket_index_by_fee_rate` in `util/fee-estimator/src/estimator/confirmation_fraction.rs` through two production paths from a local miner process selecting proposals and uncles near limit boundaries and make one path accept while the other rejects because of verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/fee-estimator/src/estimator/confirmation_fraction.rs::bucket_index_by_fee_rate`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
