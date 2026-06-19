# Q3636: High txpool batch interaction bug in estimate

## Question
Can an unprivileged attacker batch block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state so `estimate` in `util/fee-estimator/src/estimator/confirmation_fraction.rs` handles the first item safely but applies incorrect assumptions to later items and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/fee-estimator/src/estimator/confirmation_fraction.rs::estimate`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
