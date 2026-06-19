# Q1675: Critical network limit off by one in try_nat_traversal

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses through a transaction/block relayer sending repeated malformed-but-cheap payloads so `try_nat_traversal` in `network/src/protocols/hole_punching/component/connection_request_delivered.rs` cause high CPU or memory work before frame/message limits and peer punishment are applied, violating sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/protocols/hole_punching/component/connection_request_delivered.rs::try_nat_traversal`
- Entrypoint: a transaction/block relayer sending repeated malformed-but-cheap payloads
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
