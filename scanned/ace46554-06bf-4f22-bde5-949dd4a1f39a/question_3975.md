# Q3975: Critical vm limit off by one in is_write

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes so `is_write` in `script/src/types.rs` undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions, violating cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/types.rs::is_write`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions
- Invariant to test: cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
