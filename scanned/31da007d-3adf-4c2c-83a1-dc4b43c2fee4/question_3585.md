# Q3585: Critical txpool differential path split in fresh_proposals_filter

## Question
Can an unprivileged attacker reach `fresh_proposals_filter` in `tx-pool/src/service.rs` through two production paths from a peer relaying transactions that race recent-reject, orphan, and verification-queue state and make one path accept while the other rejects because of transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/service.rs::fresh_proposals_filter`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
