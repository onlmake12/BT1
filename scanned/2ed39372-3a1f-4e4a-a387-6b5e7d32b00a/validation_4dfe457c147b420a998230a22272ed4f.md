### Title
Attacker-Controlled `declared_cycles` Used as `max_cycles` Causes `ExceededMaximumCycles` to Be Misclassified as Malformed Transaction, Enabling Transaction Relay DoS - (File: tx-pool/src/process.rs)

### Summary
In `tx-pool/src/process.rs`, the `_process_tx` function uses the peer-supplied `declared_cycles` directly as `max_cycles` for script verification. When a remote peer