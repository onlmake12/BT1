# Q3428: Critical txpool replay reorder race in register_reject

## Question
Can an unprivileged attacker replay, reorder, or delay block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a local miner process selecting proposals and uncles near limit boundaries so `register_reject` in `tx-pool/src/callback.rs` takes a stale branch and force quadratic graph or selection behavior with few low-cost transactions, breaking the invariant that block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/callback.rs::register_reject`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
