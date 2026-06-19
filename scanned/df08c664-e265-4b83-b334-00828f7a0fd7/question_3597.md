# Q3597: Critical txpool replay reorder race in non_contextual_verify

## Question
Can an unprivileged attacker replay, reorder, or delay verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a peer relaying transactions that race recent-reject, orphan, and verification-queue state so `non_contextual_verify` in `tx-pool/src/util.rs` takes a stale branch and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, breaking the invariant that valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `tx-pool/src/util.rs::non_contextual_verify`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
