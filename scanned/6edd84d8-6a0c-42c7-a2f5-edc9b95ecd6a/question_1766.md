# Q1766: High network differential path split in check_proxy_url

## Question
Can an unprivileged attacker reach `check_proxy_url` in `network/src/proxy.rs` through two production paths from a discovery peer advertising adversarial addresses and node records and make one path accept while the other rejects because of header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses, violating P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `network/src/proxy.rs::check_proxy_url`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
