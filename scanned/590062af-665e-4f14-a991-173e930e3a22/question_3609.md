# Q3609: Critical txpool limit off by one in refresh_status

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a peer relaying transactions that race recent-reject, orphan, and verification-queue state so `refresh_status` in `tx-pool/src/verify_mgr.rs` make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/verify_mgr.rs::refresh_status`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
