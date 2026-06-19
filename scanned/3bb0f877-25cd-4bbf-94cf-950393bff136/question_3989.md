# Q3989: High vm differential path split in verify_script_group

## Question
Can an unprivileged attacker reach `verify_script_group` in `script/src/verify.rs` through two production paths from a transaction sender deploying a crafted CKB-VM script and witness payload and make one path accept while the other rejects because of spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing, violating cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/verify.rs::verify_script_group`
- Entrypoint: a transaction sender deploying a crafted CKB-VM script and witness payload
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions
- Invariant to test: cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
