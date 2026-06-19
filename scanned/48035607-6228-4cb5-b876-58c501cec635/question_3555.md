# Q3555: Critical txpool parser precheck gap in load_from_file

## Question
Can an unprivileged attacker submit malformed-but-reachable duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions so `load_from_file` in `tx-pool/src/persisted.rs` performs expensive or unsafe work before validation and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/persisted.rs::load_from_file`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
