# Q3349: Critical txpool parser precheck gap in from

## Question
Can an unprivileged attacker submit malformed-but-reachable duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions so `from` in `miner/src/lib.rs` performs expensive or unsafe work before validation and force quadratic graph or selection behavior with few low-cost transactions, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `miner/src/lib.rs::from`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
