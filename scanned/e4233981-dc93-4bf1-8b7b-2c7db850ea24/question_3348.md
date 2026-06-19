# Q3348: Critical txpool restart reorg persistence in from

## Question
Can an unprivileged attacker shape transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions, then force normal restart, reorg, retry, or replay handling so `from` in `miner/src/lib.rs` persists inconsistent state and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `miner/src/lib.rs::from`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
