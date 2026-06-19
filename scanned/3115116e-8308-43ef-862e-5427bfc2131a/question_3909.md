# Q3909: High vm resource amplification in new

## Question
Can an unprivileged attacker repeatedly send small spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes to make `new` in `script/src/syscalls/read.rs` amplify CPU, memory, storage, or bandwidth and undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/read.rs::new`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
