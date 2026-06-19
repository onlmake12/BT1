# Q3490: High txpool boundary divergence in pending_size

## Question
Can an unprivileged attacker enter through a local miner process selecting proposals and uncles near limit boundaries and use transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status to drive `pending_size` in `tx-pool/src/component/pool_map.rs` across a boundary where make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating the invariant that valid user transactions must not be persistently censored by cheap attacker-created pool state, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/component/pool_map.rs::pending_size`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
