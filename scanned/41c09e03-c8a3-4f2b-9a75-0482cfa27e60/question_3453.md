# Q3453: Critical txpool state transition mismatch in get_children

## Question
Can an unprivileged attacker enter through a peer relaying transactions that race recent-reject, orphan, and verification-queue state and sequence duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions so `get_children` in `tx-pool/src/component/links.rs` observes pre-state and post-state from different views, letting the flow force quadratic graph or selection behavior with few low-cost transactions, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/component/links.rs::get_children`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
