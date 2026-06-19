# Q3361: Critical txpool cache invalidation failure in TryFrom

## Question
Can an unprivileged attacker use a local miner process selecting proposals and uncles near limit boundaries to alternate valid and invalid verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing so `TryFrom` in `miner/src/worker/dummy.rs` leaves a cache, index, or status flag stale and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `miner/src/worker/dummy.rs::TryFrom`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
