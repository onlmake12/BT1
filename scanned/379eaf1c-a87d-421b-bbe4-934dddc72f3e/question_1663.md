# Q1663: Critical network resource amplification in new

## Question
Can an unprivileged attacker repeatedly send small header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses through a discovery peer advertising adversarial addresses and node records to make `new` in `network/src/protocols/hole_punching/component/connection_request.rs` amplify CPU, memory, storage, or bandwidth and trigger a parser or protocol-state panic with a single malformed peer-controlled payload, violating sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/protocols/hole_punching/component/connection_request.rs::new`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: trigger a parser or protocol-state panic with a single malformed peer-controlled payload
- Invariant to test: sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
