# Q3994: High vm restart reorg persistence in epoch

## Question
Can an unprivileged attacker shape spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes, then force normal restart, reorg, retry, or replay handling so `epoch` in `script/src/verify_env.rs` persists inconsistent state and trigger a VM panic or host-side bounds error before the transaction is rejected, violating cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/verify_env.rs::epoch`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
