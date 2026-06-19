# Q1837: Critical network parser precheck gap in handle_notify_log

## Question
Can an unprivileged attacker submit malformed-but-reachable message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths through a remote P2P peer sending crafted framed messages so `handle_notify_log` in `notify/src/lib.rs` performs expensive or unsafe work before validation and make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse, violating sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `notify/src/lib.rs::handle_notify_log`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: sync/relay state must not accept invalid messages or reject valid data because of peer ordering tricks
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
