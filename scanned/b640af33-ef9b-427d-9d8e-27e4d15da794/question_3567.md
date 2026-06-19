# Q3567: High txpool restart reorg persistence in record_conflict

## Question
Can an unprivileged attacker shape block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions, then force normal restart, reorg, retry, or replay handling so `record_conflict` in `tx-pool/src/pool.rs` persists inconsistent state and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/pool.rs::record_conflict`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
