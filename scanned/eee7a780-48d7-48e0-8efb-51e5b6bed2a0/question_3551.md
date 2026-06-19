# Q3551: High txpool resource amplification in TxPool

## Question
Can an unprivileged attacker repeatedly send small block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions to make `TxPool` in `tx-pool/src/persisted.rs` amplify CPU, memory, storage, or bandwidth and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/persisted.rs::TxPool`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
