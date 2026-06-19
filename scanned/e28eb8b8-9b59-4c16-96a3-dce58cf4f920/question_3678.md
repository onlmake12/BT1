# Q3678: Critical txpool limit off by one in gap

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions so `gap` in `util/proposal-table/src/lib.rs` pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/proposal-table/src/lib.rs::gap`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
