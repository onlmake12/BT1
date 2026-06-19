# Q3650: Critical txpool differential path split in target_blocks_for_estimate_mode

## Question
Can an unprivileged attacker reach `target_blocks_for_estimate_mode` in `util/fee-estimator/src/estimator/mod.rs` through two production paths from a local miner process selecting proposals and uncles near limit boundaries and make one path accept while the other rejects because of block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fee-estimator/src/estimator/mod.rs::target_blocks_for_estimate_mode`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
