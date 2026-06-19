# Q3637: Critical txpool parser precheck gap in new

## Question
Can an unprivileged attacker submit malformed-but-reachable duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions through a local miner process selecting proposals and uncles near limit boundaries so `new` in `util/fee-estimator/src/estimator/confirmation_fraction.rs` performs expensive or unsafe work before validation and force quadratic graph or selection behavior with few low-cost transactions, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/fee-estimator/src/estimator/confirmation_fraction.rs::new`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
