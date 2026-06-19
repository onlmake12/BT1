# Q3399: Critical txpool boundary divergence in new

## Question
Can an unprivileged attacker enter through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions and use block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples to drive `new` in `tx-pool/src/block_assembler/candidate_uncles.rs` across a boundary where force quadratic graph or selection behavior with few low-cost transactions, violating the invariant that valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `tx-pool/src/block_assembler/candidate_uncles.rs::new`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
