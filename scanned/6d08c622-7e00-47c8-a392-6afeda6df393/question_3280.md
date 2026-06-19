# Q3280: Critical transaction parser precheck gap in new_mirana

## Question
Can an unprivileged attacker submit malformed-but-reachable canonical cell status before and after reorg, snapshot lookup results, and dep-group layout through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values so `new_mirana` in `util/types/src/core/hardfork/mod.rs` performs expensive or unsafe work before validation and create a state transition where capacity or spendability changes without a matching valid authorization, violating resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/hardfork/mod.rs::new_mirana`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: create a state transition where capacity or spendability changes without a matching valid authorization
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
