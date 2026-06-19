# Q3696: High vm batch interaction bug in test_downcast_error_to_vm_error

## Question
Can an unprivileged attacker batch cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes so `test_downcast_error_to_vm_error` in `script/src/error.rs` handles the first item safely but applies incorrect assumptions to later items and trigger a VM panic or host-side bounds error before the transaction is rejected, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/error.rs::test_downcast_error_to_vm_error`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
