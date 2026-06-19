# Q1456: Critical network parser precheck gap in Display

## Question
Can an unprivileged attacker submit malformed-but-reachable header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses through a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks so `Display` in `network/src/errors.rs` performs expensive or unsafe work before validation and cause high CPU or memory work before frame/message limits and peer punishment are applied, violating peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/errors.rs::Display`
- Entrypoint: a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
