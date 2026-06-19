# Q3645: Critical txpool parser precheck gap in new_confirmation_fraction

## Question
Can an unprivileged attacker submit malformed-but-reachable verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a peer relaying transactions that race recent-reject, orphan, and verification-queue state so `new_confirmation_fraction` in `util/fee-estimator/src/estimator/mod.rs` performs expensive or unsafe work before validation and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fee-estimator/src/estimator/mod.rs::new_confirmation_fraction`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
