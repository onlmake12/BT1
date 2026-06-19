# Q3325: Critical transaction cross module inconsistency in output_pts

## Question
Can an unprivileged attacker use a block relayer including dependency-heavy transactions in an otherwise valid block to make `output_pts` in `util/types/src/core/views.rs` return a result that downstream modules interpret differently, where make dependency resolution use a different cell/header than the script-visible authorization path, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/views.rs::output_pts`
- Entrypoint: a block relayer including dependency-heavy transactions in an otherwise valid block
- Attacker controls: input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
