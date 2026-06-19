# Q3342: Critical txpool batch interaction bug in From

## Question
Can an unprivileged attacker batch verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state so `From` in `miner/src/lib.rs` handles the first item safely but applies incorrect assumptions to later items and make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `miner/src/lib.rs::From`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
