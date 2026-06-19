# Q3486: Critical txpool differential path split in get_by_id

## Question
Can an unprivileged attacker reach `get_by_id` in `tx-pool/src/component/pool_map.rs` through two production paths from a local miner process selecting proposals and uncles near limit boundaries and make one path accept while the other rejects because of verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/component/pool_map.rs::get_by_id`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
