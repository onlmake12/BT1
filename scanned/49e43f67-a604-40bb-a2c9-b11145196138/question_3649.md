# Q3649: Critical txpool cache invalidation failure in reject_tx

## Question
Can an unprivileged attacker use a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions to alternate valid and invalid transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status so `reject_tx` in `util/fee-estimator/src/estimator/mod.rs` leaves a cache, index, or status flag stale and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fee-estimator/src/estimator/mod.rs::reject_tx`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
