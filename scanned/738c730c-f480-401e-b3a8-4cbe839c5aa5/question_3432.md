# Q3432: High txpool batch interaction bug in Edges

## Question
Can an unprivileged attacker batch verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a peer relaying transactions that race recent-reject, orphan, and verification-queue state so `Edges` in `tx-pool/src/component/edges.rs` handles the first item safely but applies incorrect assumptions to later items and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/component/edges.rs::Edges`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
