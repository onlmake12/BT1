Audit Report

## Title
Local RPC Duplicate Transaction Triggers Spurious `TxVerificationResult::Ok` Broadcast to All Peers - (File: `tx-pool/src/process.rs`)

## Summary
In `tx-pool/src/process.rs`, the `after_process` function's local-RPC branch emits `TxVerificationResult::Ok { original_peer: None, tx_hash }` when a transaction is rejected as `Reject::Duplicated`. This is the identical signal used for a genuinely newly-accepted transaction. The relayer's `send_bulk_of_tx_hashes` then broadcasts the tx hash to every connected full-relay peer and calls `mark_as_known_tx`, even though the transaction was already in the pool. A local RPC caller can exploit this to drive a sustained P2P broadcast amplification loop at negligible cost.

## Finding Description
In `after_process` (`tx-pool/src/process.rs` lines 538–545), the `remote = None` branch handles `Reject::Duplicated` by sending `TxVerificationResult::Ok`:

```rust
Err(Reject::Duplicated(_)) => {
    debug!("after_process {} duplicated", tx_hash);
    // re-broadcast tx when it's duplicated and submitted through local rpc
    self.send_result_to_relayer(TxVerificationResult::Ok {
        original_peer: None,
        tx_hash,
    });
}
``` [1](#0-0) 

This is structurally identical to the genuine acceptance path at lines 530–535: [2](#0-1) 

In `send_bulk_of_tx_hashes` (`sync/src/relayer/mod.rs` lines 662–669), the `original_peer: None` branch pushes the tx hash into the broadcast queue for **every** connected peer and calls `mark_as_known_tx`: [3](#0-2) 

The asymmetry with the remote path is confirmed: `is_allowed_relay()` returns `true` for `Reject::Duplicated` (it is neither `LowFeeRate` nor a malformed tx), so a remote-peer duplicate falls into the `Err(reject)` branch at lines 517–521 and emits `TxVerificationResult::Reject`, which calls `remove_from_known_txs` instead: [4](#0-3) [5](#0-4) 

Summary of asymmetry:
- **Remote duplicate** → `TxVerificationResult::Reject` → `remove_from_known_txs` (correct)
- **Local duplicate** → `TxVerificationResult::Ok` → broadcast to all peers + `mark_as_known_tx` (incorrect)

## Impact Explanation
Each duplicate `send_transaction` RPC call enqueues a `TxVerificationResult::Ok` result. On every `send_bulk_of_tx_hashes` timer tick, the node sends `RelayTransactionHashes([tx_hash])` to every connected full-relay peer. Each peer responds with `GetRelayTransactions([tx_hash])`, consuming inbound bandwidth and CPU on both sides. Repeating in a tight loop scales the amplification linearly with the number of connected peers. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (High, 10001–15000 points).

## Likelihood Explanation
The entry point is the `send_transaction` JSON-RPC method. The RPC binds to `localhost` by default, so the attacker must be a local user or any process on the same host (wallet, script, compromised dependency). No special privileges, keys, or network position are required — only the ability to call `send_transaction` with a transaction already in the pool. The attack is trivially repeatable in a tight loop with a single valid transaction.

## Recommendation
Remove the `TxVerificationResult::Ok` emission for the `Reject::Duplicated` local case. If intentional re-broadcast is desired, introduce a dedicated `TxVerificationResult::Rebroadcast` variant that enqueues the hash for peers that have not yet seen it, but does **not** call `mark_as_known_tx` and is not rate-unlimited by the duplicate submission path.

```rust
// Current (incorrect):
Err(Reject::Duplicated(_)) => {
    self.send_result_to_relayer(TxVerificationResult::Ok {
        original_peer: None,
        tx_hash,
    });
}

// Fix: no-op, or a dedicated Rebroadcast variant that skips mark_as_known_tx
Err(Reject::Duplicated(_)) => {
    debug!("after_process {} duplicated, skipping rebroadcast", tx_hash);
    // no signal emitted
}
```

## Proof of Concept
1. Start a CKB node with default RPC on `localhost:8114` and at least one connected full-relay peer.
2. Submit a valid transaction `T` via `send_transaction` RPC → accepted into the pool (returns tx hash).
3. Immediately call `send_transaction` again with the same `T` → RPC returns `PoolRejectedDuplicatedTransaction` to the caller.
4. Internally, `after_process` fires `TxVerificationResult::Ok { original_peer: None, tx_hash: T.hash() }` into the relay result queue.
5. On the next `send_bulk_of_tx_hashes` timer tick, the node sends `RelayTransactionHashes([T.hash()])` to every connected peer and calls `mark_as_known_tx(T.hash())`.
6. Each connected peer issues `GetRelayTransactions([T.hash()])` back to the node.
7. Repeat step 3 in a tight loop; each iteration re-enqueues a broadcast, amplifying outbound and inbound traffic proportionally to the number of connected peers.

### Citations

**File:** tx-pool/src/process.rs (L517-521)
```rust
                        if reject.is_allowed_relay() {
                            self.send_result_to_relayer(TxVerificationResult::Reject {
                                tx_hash: tx_hash.clone(),
                            });
                        }
```

**File:** tx-pool/src/process.rs (L530-536)
```rust
                    Ok(_) => {
                        debug!("after_process local send_result_to_relayer {}", tx_hash);
                        self.send_result_to_relayer(TxVerificationResult::Ok {
                            original_peer: None,
                            tx_hash,
                        });
                        self.process_orphan_tx(&tx).await;
```

**File:** tx-pool/src/process.rs (L538-545)
```rust
                    Err(Reject::Duplicated(_)) => {
                        debug!("after_process {} duplicated", tx_hash);
                        // re-broadcast tx when it's duplicated and submitted through local rpc
                        self.send_result_to_relayer(TxVerificationResult::Ok {
                            original_peer: None,
                            tx_hash,
                        });
                    }
```

**File:** sync/src/relayer/mod.rs (L662-670)
```rust
                                None => {
                                    // since this tx is submitted through local rpc, it is assumed to be a new tx for all connected peers
                                    let hashes = selected
                                        .entry(*target)
                                        .or_insert_with(|| Vec::with_capacity(BUFFER_SIZE));
                                    hashes.push(tx_hash.clone());
                                    self.shared.state().mark_as_known_tx(tx_hash.clone());
                                }
                            }
```

**File:** util/types/src/core/tx_pool.rs (L110-113)
```rust
    pub fn is_allowed_relay(&self) -> bool {
        matches!(self, Reject::DeclaredWrongCycles(..))
            || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
    }
```
