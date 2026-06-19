# Q1768: High network boundary divergence in check_proxy_url

## Question
Can an unprivileged attacker enter through a remote P2P peer sending crafted framed messages and use peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing to drive `check_proxy_url` in `network/src/proxy.rs` across a boundary where trigger a parser or protocol-state panic with a single malformed peer-controlled payload, violating the invariant that P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `network/src/proxy.rs::check_proxy_url`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing
- Exploit idea: trigger a parser or protocol-state panic with a single malformed peer-controlled payload
- Invariant to test: P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
