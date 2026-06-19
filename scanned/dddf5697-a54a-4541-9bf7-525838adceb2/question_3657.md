# Q3657: Critical txpool limit off by one in new_from_entry_info

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state so `new_from_entry_info` in `util/fee-estimator/src/estimator/weight_units_flow.rs` make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fee-estimator/src/estimator/weight_units_flow.rs::new_from_entry_info`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
