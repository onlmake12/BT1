# Q3560: Critical txpool differential path split in save_into_file

## Question
Can an unprivileged attacker reach `save_into_file` in `tx-pool/src/persisted.rs` through two production paths from a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions and make one path accept while the other rejects because of verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/persisted.rs::save_into_file`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
