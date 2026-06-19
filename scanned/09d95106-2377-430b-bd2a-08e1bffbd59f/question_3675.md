# Q3675: Critical txpool differential path split in finalize

## Question
Can an unprivileged attacker reach `finalize` in `util/proposal-table/src/lib.rs` through two production paths from a peer relaying transactions that race recent-reject, orphan, and verification-queue state and make one path accept while the other rejects because of block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/proposal-table/src/lib.rs::finalize`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
