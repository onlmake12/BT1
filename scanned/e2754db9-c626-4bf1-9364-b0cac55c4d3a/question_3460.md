# Q3460: Critical txpool cross module inconsistency in remove_child

## Question
Can an unprivileged attacker use a peer relaying transactions that race recent-reject, orphan, and verification-queue state to make `remove_child` in `tx-pool/src/component/links.rs` return a result that downstream modules interpret differently, where force quadratic graph or selection behavior with few low-cost transactions, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/component/links.rs::remove_child`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
