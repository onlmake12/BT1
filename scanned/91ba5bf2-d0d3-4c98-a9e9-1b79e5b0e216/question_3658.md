# Q3658: Critical txpool boundary divergence in partial_cmp

## Question
Can an unprivileged attacker enter through a peer relaying transactions that race recent-reject, orphan, and verification-queue state and use transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status to drive `partial_cmp` in `util/fee-estimator/src/estimator/weight_units_flow.rs` across a boundary where make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating the invariant that pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fee-estimator/src/estimator/weight_units_flow.rs::partial_cmp`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
