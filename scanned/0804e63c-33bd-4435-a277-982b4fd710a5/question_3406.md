# Q3406: High txpool cache invalidation failure in checked_entries_size

## Question
Can an unprivileged attacker use a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions to alternate valid and invalid block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples so `checked_entries_size` in `tx-pool/src/block_assembler/mod.rs` leaves a cache, index, or status flag stale and make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/block_assembler/mod.rs::checked_entries_size`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
