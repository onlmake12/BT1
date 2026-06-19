# Q3646: High txpool cache invalidation failure in new_confirmation_fraction

## Question
Can an unprivileged attacker use a miner/RPC block-template caller assembling blocks from adversarial tx-pool state to alternate valid and invalid duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions so `new_confirmation_fraction` in `util/fee-estimator/src/estimator/mod.rs` leaves a cache, index, or status flag stale and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/fee-estimator/src/estimator/mod.rs::new_confirmation_fraction`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
