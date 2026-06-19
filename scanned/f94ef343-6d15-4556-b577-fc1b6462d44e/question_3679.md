# Q3679: High txpool parser precheck gap in new

## Question
Can an unprivileged attacker submit malformed-but-reachable duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions so `new` in `util/proposal-table/src/lib.rs` performs expensive or unsafe work before validation and make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/proposal-table/src/lib.rs::new`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
