# Q3735: Critical vm cache invalidation failure in initialize

## Question
Can an unprivileged attacker use a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes to alternate valid and invalid malformed buffers, return codes, memory pointers, and script group composition so `initialize` in `script/src/syscalls/current_cycles.rs` leaves a cache, index, or status flag stale and make VM version gating select the wrong behavior at a hardfork boundary, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/current_cycles.rs::initialize`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
