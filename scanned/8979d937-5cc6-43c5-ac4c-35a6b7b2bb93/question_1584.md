# Q1584: Critical network batch interaction bug in fetch_random_addrs

## Question
Can an unprivileged attacker batch message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths through a remote P2P peer sending crafted framed messages so `fetch_random_addrs` in `network/src/peer_store/peer_store_impl.rs` handles the first item safely but applies incorrect assumptions to later items and cause high CPU or memory work before frame/message limits and peer punishment are applied, violating sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/peer_store/peer_store_impl.rs::fetch_random_addrs`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
