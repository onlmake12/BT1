# Q3601: High txpool restart reorg persistence in Clone

## Question
Can an unprivileged attacker shape duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions through a peer relaying transactions that race recent-reject, orphan, and verification-queue state, then force normal restart, reorg, retry, or replay handling so `Clone` in `tx-pool/src/verify_mgr.rs` persists inconsistent state and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/verify_mgr.rs::Clone`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
