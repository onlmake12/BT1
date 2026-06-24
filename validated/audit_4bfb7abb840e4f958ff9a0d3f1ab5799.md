Audit Report

## Title
Tx-Pool Admits Permanently Uncommittable Transactions via Unchecked Peer-Declared Cycles Exceeding `max_block_cycles()` — (`tx-pool/src/process.rs`)

## Summary
In `_process_tx`, the cycle ceiling passed to `verify_rtx` is taken directly from a peer-supplied `declared_cycles` value with no upper-bound check against `consensus.max_block_cycles()`. An attacker can relay a transaction whose script genuinely consumes `C > max_block_cycles()` cycles with `declared_cycles = C`, causing the transaction to pass all pool admission checks and be inserted into the pending pool. Because block validation enforces a hard aggregate cycle cap, such a transaction can never be committed to any block, permanently occupying pool space and wasting CPU on every relaying node.

## Finding Description
In `_process_tx` (`tx-pool/src/process.rs`, line 720), the cycle limit is set as:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

When a remote peer relays a transaction, `remote: Option<(Cycle, PeerIndex)>` flows through `submit_remote_tx` → `resumeble_process_tx` → `enqueue_verify_queue` → `_process_tx` as `declared_cycles: Option<Cycle>`. No check is performed to ensure `declared_cycles ≤ consensus.max_block_cycles()` before it is used as `max_cycles`.

`verify_rtx` is then called with this unclamped limit (`tx-pool/src/process.rs`, lines 724–732), allowing the script to run for arbitrarily many cycles.

The only post-verification guard (`tx-pool/src/process.rs`, lines 736–749) is a mismatch check: if `declared != verified.cycles`, the transaction is rejected with `DeclaredWrongCycles`. If the attacker sets `declared_cycles = C` and the script actually consumes exactly `C` cycles, `declared == verified.cycles` and this check passes cleanly.

The `non_contextual_verify` path (`tx-pool/src/util.rs`, lines 56–83) checks version, size, cellbase status, and duplicate deps — no cycle cap is enforced there.

The `large_cycle_threshold` field in `VerifyQueue` (`tx-pool/src/component/verify_queue.rs`, lines 63–65) is used only to classify transactions for prioritization, not as a hard rejection limit.

Block validation enforces the hard cap at commit time (`verification/contextual/src/contextual_block_verifier.rs`, lines 468–470):

```rust
if sum > self.context.consensus.max_block_cycles() {
    Err(BlockErrorKind::ExceededMaximumCycles.into())
```

A single transaction with `cycles > max_block_cycles()` makes any block's cycle sum exceed the cap, so it can never be committed. The tx-pool has no analogous per-transaction cap check at admission time.

## Impact Explanation
This maps to the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

- **Pool pollution**: Permanently uncommittable transactions occupy pool space. The pool enforces `max_tx_pool_size` and evicts by fee rate, so these transactions displace legitimate ones.
- **Wasted CPU**: Each relaying node runs the script for `> max_block_cycles()` cycles (3.5 billion+ on mainnet) per transaction, consuming significant CPU per relay hop.
- **Block-assembly interference**: If the block assembler selects such a transaction, it produces a template that fails consensus validation, wasting miner work.

## Likelihood Explanation
The attack is reachable by any unprivileged P2P peer via the standard `RelayV3` protocol. The attacker must: (1) control a UTXO with sufficient fee, (2) deploy or reference a script that provably consumes `C > max_block_cycles()` cycles (a simple tight RISC-V loop). The same script cell can be reused across many attack transactions, making the per-transaction cost only the transaction fee. No privileged access, key material, or majority hash power is required.

## Recommendation
Add an explicit guard in `_process_tx` (or at the verify-queue admission point) that rejects any transaction whose `declared_cycles` exceeds `consensus.max_block_cycles()`:

```rust
if let Some(declared) = declared_cycles {
    if declared > self.consensus.max_block_cycles() {
        return Some((
            Err(Reject::DeclaredWrongCycles(declared, self.consensus.max_block_cycles())),
            snapshot,
        ));
    }
}
```

Alternatively, clamp `max_cycles` to `min(declared_cycles, consensus.max_block_cycles())` so that any transaction whose actual cycles exceed the block cap is caught by the existing `DeclaredWrongCycles` path.

## Proof of Concept
1. Attacker owns UTXO `U` on CKB testnet.
2. Attacker deploys a lock script that loops until it has consumed `max_block_cycles() + 1` cycles (a tight RISC-V counter loop).
3. Attacker constructs transaction `T` spending `U`, locked by that script, with a fee satisfying `min_fee_rate`.
4. Attacker connects to a target node via `RelayV3` and sends a `RelayTransactionV3` message with `cycles = max_block_cycles() + 1`.
5. The node calls `resumeble_process_tx` → `_process_tx` with `declared_cycles = max_block_cycles() + 1`.
6. `max_cycles = max_block_cycles() + 1`; `verify_rtx` runs the script with this limit; the script completes in exactly that many cycles.
7. `declared == verified.cycles` → no `DeclaredWrongCycles` rejection.
8. `T` is inserted into the pending pool.
9. Any block containing `T` has `sum ≥ max_block_cycles() + 1`, failing `ExceededMaximumCycles` in `contextual_block_verifier.rs`.
10. Repeating with many UTXOs (reusing the same script cell) fills the pool, evicting legitimate transactions.