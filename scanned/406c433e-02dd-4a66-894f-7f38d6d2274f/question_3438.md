# Q3438: Critical txpool boundary divergence in inputs_len

## Question
Can an unprivileged attacker enter through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions and use transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status to drive `inputs_len` in `tx-pool/src/component/edges.rs` across a boundary where make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating the invariant that pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/component/edges.rs::inputs_len`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
