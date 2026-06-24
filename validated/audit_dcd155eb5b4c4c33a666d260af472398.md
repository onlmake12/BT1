Audit Report

## Title
Local RPC Duplicate Transaction Triggers Spurious `TxVerificationResult::Ok` Broadcast to All Peers - (File: `tx-pool/src/process.rs`)

## Summary
In `tx-pool/src/process.rs`, the `after_process` function's local-RPC branch (`remote = None`) emits `TxVerificationResult::Ok { original_peer: None, tx_hash }` when a transaction is rejected as `Reject::Duplicated`. This is the identical signal used for a genuinely newly-accepted transaction. The relayer in `sync/src/relayer/mod.rs` then broadcasts the tx hash to every connected full-relay peer and calls `mark_as_known_tx`, despite the transaction already being in the pool. A local RPC caller can exploit this by repeatedly submitting the same transaction to drive unnecessary hash announcements and peer-side `GetRelayTransactions` requests.

## Finding Description
The root cause is in `tx-pool/src/process.rs` lines 538–545:

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

This is structurally identical to the genuine acceptance path at lines 530–535, which also emits `TxVerificationResult::Ok { original_peer: None, tx_hash }`. [2](#0-1) 

In `sync/src/relayer/mod.rs`, `send_bulk_of_tx_hashes` processes this result. The `original_peer: None` branch pushes the tx hash into the broadcast queue for **every** connected peer and calls `mark_as_known_tx`: [3](#0-2) 

The asymmetry with the remote path is confirmed: when a remote peer sends a duplicate, `Reject::Duplicated` falls into the generic `Err(reject)` branch (lines 502–526), and if `is_allowed_relay()` is true, the relayer receives `TxVerificationResult::Reject`, which calls `remove_from_known_txs` instead. [4](#0-3) 

`is_allowed_relay()` for `Reject::Duplicated` evaluates to `true` (it is not `LowFeeRate` and not a malformed tx), confirming the remote path sends `Reject` while the local path sends `Ok`. [5](#0-4) 

No existing guard prevents repeated local RPC submissions from enqueuing multiple `TxVerificationResult::Ok` entries into the relay result queue.

## Impact Explanation
Each duplicate `send_transaction` RPC call causes the node to enqueue a broadcast of the tx hash to every connected full-relay peer via `RelayTransactionHashes`. Peers that do not yet have the tx in their local filter will respond with `GetRelayTransactions`, and the node will serve the full transaction bytes. A tight loop of duplicate submissions from a local process amplifies outbound hash announcements and inbound transaction requests proportionally to the number of connected peers. This matches the allowed impact: **High — bad design which could cause CKB network congestion with few costs**, since the attacker cost is a trivial loop of RPC calls while the network cost scales with peer count.

## Likelihood Explanation
The entry point is the `send_transaction` JSON-RPC method. By default the RPC binds to `localhost:8114`, so any local process (wallet, script, compromised dependency) with access to that port can trigger this. No keys, special privileges, or network position are required. The attack is repeatable at the rate of RPC calls the caller can issue, and each call independently enqueues a new broadcast event.

## Recommendation
Replace the `TxVerificationResult::Ok` emission for the `Reject::Duplicated` local case with either a no-op or a dedicated `TxVerificationResult::Rebroadcast` variant. The dedicated variant would allow `send_bulk_of_tx_hashes` to re-announce the hash without calling `mark_as_known_tx` and without treating the tx as a brand-new acceptance. At minimum, the current code must not emit `Ok` for an already-pooled transaction:

```rust
// Suggested: no-op, or a dedicated Rebroadcast variant
Err(Reject::Duplicated(_)) => {
    debug!("after_process {} duplicated", tx_hash);
    // do not re-signal Ok; tx is already in pool
}
```

## Proof of Concept
1. Start a CKB node with default RPC on `localhost:8114` and at least one connected full-relay peer.
2. Submit a valid transaction `T` via `send_transaction` → accepted into the pool.
3. Immediately call `send_transaction` again with the same `T` in a tight loop.
4. Each call returns `PoolRejectedDuplicatedTransaction` to the caller, but internally `after_process` fires `TxVerificationResult::Ok { original_peer: None, tx_hash: T.hash() }`.
5. On each `send_bulk_of_tx_hashes` timer tick, the node sends `RelayTransactionHashes([T.hash()])` to every connected peer.
6. Connected peers that lack `T` in their filter respond with `GetRelayTransactions([T.hash()])`, consuming bandwidth on both sides.
7. Capture traffic with `tcpdump` or a CKB peer stub to confirm repeated `RelayTransactionHashes` messages containing the already-pooled tx hash.

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
