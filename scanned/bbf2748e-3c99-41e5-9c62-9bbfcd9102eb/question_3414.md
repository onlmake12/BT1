# Q3414: Critical txpool resource amplification in process

## Question
Can an unprivileged attacker repeatedly send small block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a peer relaying transactions that race recent-reject, orphan, and verification-queue state to make `process` in `tx-pool/src/block_assembler/process.rs` amplify CPU, memory, storage, or bandwidth and make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/block_assembler/process.rs::process`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
