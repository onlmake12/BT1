# Q3384: Critical txpool cache invalidation failure in new

## Question
Can an unprivileged attacker use a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions to alternate valid and invalid verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing so `new` in `miner/src/worker/mod.rs` leaves a cache, index, or status flag stale and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `miner/src/worker/mod.rs::new`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
