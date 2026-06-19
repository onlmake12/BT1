# Q3345: Critical txpool replay reorder race in From

## Question
Can an unprivileged attacker replay, reorder, or delay block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a local miner process selecting proposals and uncles near limit boundaries so `From` in `miner/src/lib.rs` takes a stale branch and force quadratic graph or selection behavior with few low-cost transactions, breaking the invariant that valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `miner/src/lib.rs::From`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
