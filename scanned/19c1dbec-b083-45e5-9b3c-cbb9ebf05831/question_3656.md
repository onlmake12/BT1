# Q3656: Critical txpool cache invalidation failure in max_bucket_index_by_fee_rate

## Question
Can an unprivileged attacker use a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions to alternate valid and invalid block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples so `max_bucket_index_by_fee_rate` in `util/fee-estimator/src/estimator/weight_units_flow.rs` leaves a cache, index, or status flag stale and make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fee-estimator/src/estimator/weight_units_flow.rs::max_bucket_index_by_fee_rate`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
