All cited code references check out. Let me verify the `resumeble_process_tx` path to confirm the full call chain.

Audit Report

## Title
`declared_cycles` Used as Script Execution Cap Causes Asymmetric Relay Suppression for Under-Declared Cycles - (File: tx-pool/src/process.rs)

## Summary

In `_process_tx`, the peer-supplied `declared_cycles` value is used as the hard execution cap passed to `verify_rtx`. When a peer sends `declared_cycles` below the transaction's actual cycle cost, the script verifier aborts with a cycles-exceeded `ErrorKind::Script` error rather than completing and producing a `DeclaredWrongCycles` rejection. This script error is classified as a malformed-transaction error, for which `is_allowed_relay()` returns `false`, so the tx hash is never removed from the node's known-tx filter. Any unprivileged peer can permanently suppress relay of a valid transaction on a target node for the filter's TTL by relaying it with an artificially low cycle declaration.

## Finding Description

**Root cause — `tx-pool/src/process.rs`, `_process_tx` (line 720):**

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
// max_cycles == declared_cycles when Some(_)
let verified_ret = verify_rtx(..., max_cycles, ...).await;
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

if let Some(declared) = declared_cycles
    && declared != verified.cycles   // only reached when declared >= actual
{ ... DeclaredWrongCycles ... }
``` [1](#0-0) 

When `declared_cycles < actual_cycles`, `verify_rtx` terminates early with a cycles-exceeded error (`ErrorKind::Script`) before the `DeclaredWrongCycles` check is ever reached. The two cases are asymmetric:

| `declared_cycles` vs actual | Verifier outcome | Reject variant | `is_allowed_relay()` |
|---|---|---|---|
| `declared > actual` | Completes → exact-match fires | `DeclaredWrongCycles` | **true** |
| `declared < actual` | Aborts → script error | `Verification(Script)` | **false** |

**Classification chain:**

`is_malformed_from_verification` treats all `ErrorKind::Script` errors (except `ARGV_TOO_LONG_TEXT`) as malformed:

```rust
ErrorKind::Script => !format!("{}", error).contains(ARGV_TOO_LONG_TEXT),
``` [2](#0-1) 

`is_allowed_relay` returns `false` for any malformed tx that is not `DeclaredWrongCycles`:

```rust
pub fn is_allowed_relay(&self) -> bool {
    matches!(self, Reject::DeclaredWrongCycles(..))
        || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
}
``` [3](#0-2) 

**Relay suppression mechanism:**

In `TransactionsProcess::execute`, the tx hash is marked as known *before* pool submission:

```rust
shared_state.mark_as_known_txs(txs.iter().map(|(tx, _)| tx.hash()));
``` [4](#0-3) 

In `after_process`, `TxVerificationResult::Reject` (which triggers `remove_from_known_txs`) is only sent when `reject.is_allowed_relay()` is true:

```rust
if reject.is_allowed_relay() {
    self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash: tx_hash.clone() });
}
``` [5](#0-4) 

In `send_bulk_of_tx_hashes`, `TxVerificationResult::Reject` is the only path that calls `remove_from_known_txs`:

```rust
TxVerificationResult::Reject { tx_hash } => {
    self.shared.state().remove_from_known_txs(&tx_hash);
}
``` [6](#0-5) 

Because the script error is not allowed relay, the hash is never removed. The node will not request this transaction from any other peer for the duration of the filter's TTL.

**Existing guard is insufficient:**

The only guard in `TransactionsProcess::execute` rejects `declared_cycles > max_block_cycles`; there is no lower-bound check:

```rust
if txs.iter().any(|(_, declared_cycles)| declared_cycles > &max_block_cycles) {
    self.nc.ban_peer(...);
    return Status::ok();
}
``` [7](#0-6) 

**Contrast with `_test_accept_tx`**, which correctly uses `max_block_cycles` as the execution cap and never passes `declared_cycles` to `verify_rtx`:

```rust
let max_cycles = self.consensus.max_block_cycles();
``` [8](#0-7) 

## Impact Explanation

This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker using multiple IPs or Sybil connections can target many nodes simultaneously, suppressing relay of a valid transaction across a significant portion of the network for the filter's TTL window. The transaction is silently dropped on each targeted node — it will not be re-requested from any other peer — delaying or blocking confirmation. The cost to the attacker is only a standard P2P relay message per target node; the attacker is banned after each attempt but the suppression persists.

## Likelihood Explanation

The attack requires only a standard `RelayTransactions` P2P message with the `cycles` field set to any value below the transaction's actual cycle cost (e.g., `1`). No privileged access, no key material, and no majority hash power is needed. The attacker only needs to know the target transaction's hash (observable from mempool or pending proposals) and its existence on the network. The attack is repeatable across different nodes using different IPs.

## Recommendation

Use `max_block_cycles` (or `max_tx_verify_cycles`) as the execution cap in `_process_tx`, independent of `declared_cycles`. Reserve `declared_cycles` solely for the post-verification exact-match check:

```rust
// Use consensus limit for execution, not the peer-declared value
let max_cycles = self.consensus.max_block_cycles();

let verified_ret = verify_rtx(..., max_cycles, ...).await;
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

// Only now compare against declared_cycles
if let Some(declared) = declared_cycles
    && declared != verified.cycles
{
    return Some((Err(Reject::DeclaredWrongCycles(declared, verified.cycles)), snapshot));
}
```

This matches the behavior of `_test_accept_tx` and ensures that a peer declaring too-low cycles receives `DeclaredWrongCycles` (re-relay allowed) rather than a script error (re-relay suppressed).

## Proof of Concept

1. Node A has `max_block_cycles = 3_500_000_000`.
2. A valid transaction `T` requires `537` cycles (e.g., `always_success` script).
3. Attacker peer connects to Node A and sends a `RelayTransactions` message containing `T` with `cycles = 1`.
4. `TransactionsProcess::execute`: `1 <= max_block_cycles` → no ban at this stage; tx hash marked as known via `mark_as_known_txs`; `submit_remote_tx(T, declared_cycles=1, peer)` called.
5. `_process_tx`: `max_cycles = declared_cycles = 1`; `verify_rtx` runs the script with cap `1`; script aborts with cycles-exceeded (`ErrorKind::Script`).
6. `after_process`: `is_malformed_tx()` = true → attacker peer banned; `is_allowed_relay()` = false → no `TxVerificationResult::Reject` sent → `remove_from_known_txs` never called.
7. `T`'s hash remains in Node A's known-tx filter; Node A will not request `T` from any other peer for the filter's TTL.
8. Legitimate peers relaying `T` with correct cycles `537` are ignored by Node A (tx already "known").

To reproduce: modify the `DeclaredWrongCycles` integration test to relay with `cycles = 1` instead of `ALWAYS_SUCCESS_SCRIPT_CYCLE + 1`, then verify that after the attacker peer is banned, a second legitimate peer relaying `T` with correct cycles is also ignored by Node A (tx never enters the pending pool).

### Citations

**File:** tx-pool/src/process.rs (L517-521)
```rust
                        if reject.is_allowed_relay() {
                            self.send_result_to_relayer(TxVerificationResult::Reject {
                                tx_hash: tx_hash.clone(),
                            });
                        }
```

**File:** tx-pool/src/process.rs (L720-748)
```rust
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;

        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
```

**File:** tx-pool/src/process.rs (L787-787)
```rust
        let max_cycles = self.consensus.max_block_cycles();
```

**File:** util/types/src/core/tx_pool.rs (L75-75)
```rust
        ErrorKind::Script => !format!("{}", error).contains(ARGV_TOO_LONG_TEXT),
```

**File:** util/types/src/core/tx_pool.rs (L110-113)
```rust
    pub fn is_allowed_relay(&self) -> bool {
        matches!(self, Reject::DeclaredWrongCycles(..))
            || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
    }
```

**File:** sync/src/relayer/transactions_process.rs (L63-74)
```rust
        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }
```

**File:** sync/src/relayer/transactions_process.rs (L76-76)
```rust
        shared_state.mark_as_known_txs(txs.iter().map(|(tx, _)| tx.hash()));
```

**File:** sync/src/relayer/mod.rs (L673-675)
```rust
                    TxVerificationResult::Reject { tx_hash } => {
                        self.shared.state().remove_from_known_txs(&tx_hash);
                    }
```
