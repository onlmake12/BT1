# Q3363: Critical txpool state transition mismatch in default

## Question
Can an unprivileged attacker enter through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state and sequence block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples so `default` in `miner/src/worker/dummy.rs` observes pre-state and post-state from different views, letting the flow force quadratic graph or selection behavior with few low-cost transactions, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `miner/src/worker/dummy.rs::default`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
