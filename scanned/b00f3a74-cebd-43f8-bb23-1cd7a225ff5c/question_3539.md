# Q3539: Critical txpool replay reorder race in handle_send_cmd_error

## Question
Can an unprivileged attacker replay, reorder, or delay verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a local miner process selecting proposals and uncles near limit boundaries so `handle_send_cmd_error` in `tx-pool/src/error.rs` takes a stale branch and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, breaking the invariant that tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/error.rs::handle_send_cmd_error`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
