# Q3493: Critical txpool resource amplification in estimate_total_keys_num

## Question
Can an unprivileged attacker repeatedly send small verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a peer relaying transactions that race recent-reject, orphan, and verification-queue state to make `estimate_total_keys_num` in `tx-pool/src/component/recent_reject.rs` amplify CPU, memory, storage, or bandwidth and force quadratic graph or selection behavior with few low-cost transactions, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/component/recent_reject.rs::estimate_total_keys_num`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
