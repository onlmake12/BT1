# Q3518: Critical txpool boundary divergence in skip_proposed_entry

## Question
Can an unprivileged attacker enter through a local miner process selecting proposals and uncles near limit boundaries and use transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status to drive `skip_proposed_entry` in `tx-pool/src/component/tx_selector.rs` across a boundary where make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating the invariant that tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/component/tx_selector.rs::skip_proposed_entry`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
