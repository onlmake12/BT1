# Q3505: High txpool limit off by one in From

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state so `From` in `tx-pool/src/component/sort_key.rs` make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/component/sort_key.rs::From`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
