# Q3720: High vm batch interaction bug in terminated_result

## Question
Can an unprivileged attacker batch spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing through a transaction sender deploying a crafted CKB-VM script and witness payload so `terminated_result` in `script/src/scheduler.rs` handles the first item safely but applies incorrect assumptions to later items and make VM version gating select the wrong behavior at a hardfork boundary, violating CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/scheduler.rs::terminated_result`
- Entrypoint: a transaction sender deploying a crafted CKB-VM script and witness payload
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
