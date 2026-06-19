# Q3563: Critical txpool cache invalidation failure in contains_proposal_id

## Question
Can an unprivileged attacker use a miner/RPC block-template caller assembling blocks from adversarial tx-pool state to alternate valid and invalid duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions so `contains_proposal_id` in `tx-pool/src/pool.rs` leaves a cache, index, or status flag stale and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `tx-pool/src/pool.rs::contains_proposal_id`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
