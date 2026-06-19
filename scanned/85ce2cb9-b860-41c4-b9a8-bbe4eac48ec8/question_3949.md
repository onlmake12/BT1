# Q3949: High vm parser precheck gap in new

## Question
Can an unprivileged attacker submit malformed-but-reachable malformed buffers, return codes, memory pointers, and script group composition through a transaction sender deploying a crafted CKB-VM script and witness payload so `new` in `script/src/syscalls/wait.rs` performs expensive or unsafe work before validation and trigger a VM panic or host-side bounds error before the transaction is rejected, violating CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/wait.rs::new`
- Entrypoint: a transaction sender deploying a crafted CKB-VM script and witness payload
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
