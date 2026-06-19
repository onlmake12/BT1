# Q3441: Critical txpool boundary divergence in add_ancestor_weight

## Question
Can an unprivileged attacker enter through a local miner process selecting proposals and uncles near limit boundaries and use block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples to drive `add_ancestor_weight` in `tx-pool/src/component/entry.rs` across a boundary where force quadratic graph or selection behavior with few low-cost transactions, violating the invariant that pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/component/entry.rs::add_ancestor_weight`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
