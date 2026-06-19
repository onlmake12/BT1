# Q3862: High vm resource amplification in From

## Question
Can an unprivileged attacker repeatedly send small malformed buffers, return codes, memory pointers, and script group composition through a block relayer executing transactions at VM-version or hardfork activation boundaries to make `From` in `script/src/syscalls/mod.rs` amplify CPU, memory, storage, or bandwidth and make VM version gating select the wrong behavior at a hardfork boundary, violating cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/mod.rs::From`
- Entrypoint: a block relayer executing transactions at VM-version or hardfork activation boundaries
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
