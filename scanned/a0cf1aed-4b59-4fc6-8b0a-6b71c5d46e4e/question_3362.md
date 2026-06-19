# Q3362: Critical txpool batch interaction bug in Worker

## Question
Can an unprivileged attacker batch duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state so `Worker` in `miner/src/worker/dummy.rs` handles the first item safely but applies incorrect assumptions to later items and force quadratic graph or selection behavior with few low-cost transactions, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `miner/src/worker/dummy.rs::Worker`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
