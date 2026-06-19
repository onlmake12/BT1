# Q3719: Critical vm differential path split in terminated

## Question
Can an unprivileged attacker reach `terminated` in `script/src/scheduler.rs` through two production paths from a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes and make one path accept while the other rejects because of malformed buffers, return codes, memory pointers, and script group composition, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/scheduler.rs::terminated`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
