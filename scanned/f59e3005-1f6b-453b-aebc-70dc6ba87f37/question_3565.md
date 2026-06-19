# Q3565: Critical txpool resource amplification in get_tx_from_pool

## Question
Can an unprivileged attacker repeatedly send small duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions through a peer relaying transactions that race recent-reject, orphan, and verification-queue state to make `get_tx_from_pool` in `tx-pool/src/pool.rs` amplify CPU, memory, storage, or bandwidth and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `tx-pool/src/pool.rs::get_tx_from_pool`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
