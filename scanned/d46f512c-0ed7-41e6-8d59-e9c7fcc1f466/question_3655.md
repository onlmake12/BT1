# Q3655: Critical txpool limit off by one in lowest_fee_rate_by_bucket_index

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions so `lowest_fee_rate_by_bucket_index` in `util/fee-estimator/src/estimator/weight_units_flow.rs` make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fee-estimator/src/estimator/weight_units_flow.rs::lowest_fee_rate_by_bucket_index`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
