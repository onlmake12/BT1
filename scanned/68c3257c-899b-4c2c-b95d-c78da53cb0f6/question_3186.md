# Q3186: Critical transaction canonical encoding ambiguity in try_from

## Question
Can an unprivileged attacker craft alternate encodings for canonical cell status before and after reorg, snapshot lookup results, and dep-group layout through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values so `try_from` in `util/types/src/core/blockchain.rs` accepts two representations for one security object and create a state transition where capacity or spendability changes without a matching valid authorization, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/types/src/core/blockchain.rs::try_from`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: create a state transition where capacity or spendability changes without a matching valid authorization
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
