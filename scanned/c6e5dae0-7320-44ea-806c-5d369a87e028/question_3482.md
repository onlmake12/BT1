# Q3482: Critical txpool replay reorder race in add_proposed

## Question
Can an unprivileged attacker replay, reorder, or delay duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state so `add_proposed` in `tx-pool/src/component/pool_map.rs` takes a stale branch and make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, breaking the invariant that tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/component/pool_map.rs::add_proposed`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
