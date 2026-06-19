# Q3497: High txpool boundary divergence in get_shard

## Question
Can an unprivileged attacker enter through a local miner process selecting proposals and uncles near limit boundaries and use block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples to drive `get_shard` in `tx-pool/src/component/recent_reject.rs` across a boundary where make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating the invariant that valid user transactions must not be persistently censored by cheap attacker-created pool state, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/component/recent_reject.rs::get_shard`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
