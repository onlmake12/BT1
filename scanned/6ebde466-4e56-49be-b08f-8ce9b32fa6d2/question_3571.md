# Q3571: Critical txpool cache invalidation failure in fetch_txs_verify_cache

## Question
Can an unprivileged attacker use a local miner process selecting proposals and uncles near limit boundaries to alternate valid and invalid transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status so `fetch_txs_verify_cache` in `tx-pool/src/process.rs` leaves a cache, index, or status flag stale and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/process.rs::fetch_txs_verify_cache`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
