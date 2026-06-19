# Q3589: Critical txpool canonical encoding ambiguity in notify_txs_async

## Question
Can an unprivileged attacker craft alternate encodings for duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions so `notify_txs_async` in `tx-pool/src/service.rs` accepts two representations for one security object and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `tx-pool/src/service.rs::notify_txs_async`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
