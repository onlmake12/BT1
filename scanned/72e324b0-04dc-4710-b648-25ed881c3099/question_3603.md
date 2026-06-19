# Q3603: Critical txpool limit off by one in WorkerExit

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions so `WorkerExit` in `tx-pool/src/verify_mgr.rs` force quadratic graph or selection behavior with few low-cost transactions, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `tx-pool/src/verify_mgr.rs::WorkerExit`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
