# Q3354: High txpool limit off by one in notify_new_work

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state so `notify_new_work` in `miner/src/miner.rs` make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `miner/src/miner.rs::notify_new_work`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
