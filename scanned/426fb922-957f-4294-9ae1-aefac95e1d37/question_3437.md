# Q3437: High txpool restart reorg persistence in get_deps_ref

## Question
Can an unprivileged attacker shape verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state, then force normal restart, reorg, retry, or replay handling so `get_deps_ref` in `tx-pool/src/component/edges.rs` persists inconsistent state and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/component/edges.rs::get_deps_ref`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
