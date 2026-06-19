# Q3593: High txpool restart reorg persistence in check_txid_collision

## Question
Can an unprivileged attacker shape transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status through a local miner process selecting proposals and uncles near limit boundaries, then force normal restart, reorg, retry, or replay handling so `check_txid_collision` in `tx-pool/src/util.rs` persists inconsistent state and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/util.rs::check_txid_collision`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
