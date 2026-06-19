# Q3385: High txpool boundary divergence in nonce_generator

## Question
Can an unprivileged attacker enter through a local miner process selecting proposals and uncles near limit boundaries and use block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples to drive `nonce_generator` in `miner/src/worker/mod.rs` across a boundary where make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating the invariant that block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `miner/src/worker/mod.rs::nonce_generator`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
