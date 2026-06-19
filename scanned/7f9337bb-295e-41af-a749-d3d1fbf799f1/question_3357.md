# Q3357: Critical txpool differential path split in notify_workers

## Question
Can an unprivileged attacker reach `notify_workers` in `miner/src/miner.rs` through two production paths from a peer relaying transactions that race recent-reject, orphan, and verification-queue state and make one path accept while the other rejects because of verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `miner/src/miner.rs::notify_workers`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
