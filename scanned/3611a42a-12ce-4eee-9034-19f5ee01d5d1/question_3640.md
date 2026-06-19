# Q3640: Critical txpool limit off by one in remove_unconfirmed_tx

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status through a peer relaying transactions that race recent-reject, orphan, and verification-queue state so `remove_unconfirmed_tx` in `util/fee-estimator/src/estimator/confirmation_fraction.rs` make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fee-estimator/src/estimator/confirmation_fraction.rs::remove_unconfirmed_tx`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
