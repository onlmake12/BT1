# Q3980: High vm limit off by one in vm_version

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing through a block relayer executing transactions at VM-version or hardfork activation boundaries so `vm_version` in `script/src/types.rs` undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/types.rs::vm_version`
- Entrypoint: a block relayer executing transactions at VM-version or hardfork activation boundaries
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
