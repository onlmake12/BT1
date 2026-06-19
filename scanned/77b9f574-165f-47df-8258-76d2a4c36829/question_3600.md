# Q3600: Critical txpool cache invalidation failure in verify_rtx

## Question
Can an unprivileged attacker use a local miner process selecting proposals and uncles near limit boundaries to alternate valid and invalid verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing so `verify_rtx` in `tx-pool/src/util.rs` leaves a cache, index, or status flag stale and force quadratic graph or selection behavior with few low-cost transactions, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `tx-pool/src/util.rs::verify_rtx`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
