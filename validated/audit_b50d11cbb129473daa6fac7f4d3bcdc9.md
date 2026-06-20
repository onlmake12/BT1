### Title
Exact Equality Check on Declared Cycles Rejects Valid Relayed Transactions and Incorrectly Bans Peers - (File: tx-pool/src/process.rs)

### Summary
In `tx-pool/src/process.rs`, the `_process_tx` function uses an exact equality check (`declared != verified.cycles`) to validate the declared cycles of a remotely relayed transaction. This mirrors the NMKT-6 pattern: an upper-bound check should be used instead. A peer that over-declares cycles (declares more than the actual verified cycles) is submitting a conservative, safe value — the transaction still verifies correctly within the declared limit — yet the node rejects the transaction and bans the peer as malformed.

### Finding Description

When a remote peer relays a transaction via the P2P relay protocol, it includes a `declared_cycles` value. In `_process_tx`, this value is used as `max_cycles` for script verification: [1](#0-0) 

After verification completes, the node checks: [2](#0-1) 

The check `declared != verified.cycles` is an exact equality test. If a peer declares cycles = 1000 but the actual verified cycles = 999 (over-declaration), the transaction is rejected with `Reject::DeclaredWrongCycles`. Because `DeclaredWrongCycles` is classified as a malformed transaction: [3](#0-2) 

...the relaying peer is subsequently banned for 3 days (`DEFAULT_BAN_TIME`): [4](#0-3) 

Over-declaring cycles is semantically safe: the transaction verifies within the declared limit, and the actual cycles recorded in the pool entry use `verified.cycles`, not `declared`. Only under-declaration (claiming fewer cycles than actually consumed) is a protocol violation, because it would allow a transaction to bypass the `max_block_cycles` guard.

The entry path is the P2P relay handler in `sync/src/relayer/transactions_process.rs`, which calls `submit_remote_tx` with the peer-supplied `declared_cycles`: [5](#0-4) 

### Impact Explanation

Any unprivileged peer relaying a transaction with `declared_cycles` slightly above the actual verified cycles — due to conservative estimation, rounding, or minor implementation differences — will have that transaction rejected and will be banned for 72 hours. This:

1. Silently drops valid transactions from the relay network.
2. Incorrectly bans legitimate peers, degrading network connectivity.
3. Can be triggered by any tx-pool submitter or relay peer without special privileges.

### Likelihood Explanation

Moderate. The relay protocol expects exact cycle counts, and most implementations compute them precisely. However, any implementation that estimates or rounds up declared cycles (a conservative and safe practice) will trigger this. The condition is reachable by any unprivileged peer sending a `RelayTransactions` message.

### Recommendation

Replace the exact equality check with an upper-bound check. Only reject (and ban) when `declared < verified.cycles`, since under-declaration is the actual protocol violation. Over-declaration is safe and should be accepted:

```rust
// Before (exact check — rejects over-declaration):
if let Some(declared) = declared_cycles
    && declared != verified.cycles
{ ... }

// After (upper-bound check — only rejects under-declaration):
if let Some(declared) = declared_cycles
    && declared < verified.cycles
{ ... }
```

### Proof of Concept

1. Connect a custom peer to a CKB node via the `RelayV3` protocol.
2. Construct a valid transaction whose script execution consumes exactly `N` cycles (e.g., `ALWAYS_SUCCESS_SCRIPT_CYCLE = 537`).
3. Relay the transaction with `declared_cycles = N + 1` (one cycle over-declared).
4. Observe: the node rejects the transaction with `DeclaredWrongCycles(538, 537)` and bans the peer for 72 hours, even though the transaction is fully valid and verified within the declared limit.

The existing integration test `DeclaredWrongCycles` in `test/src/specs/tx_pool/declared_wrong_cycles.rs` already demonstrates the rejection path with `ALWAYS_SUCCESS_SCRIPT_CYCLE + 1`; the same setup with `declared = actual + 1` (over-declaration) reproduces this finding. [6](#0-5)

### Citations

**File:** tx-pool/src/process.rs (L698-702)
```rust
                )
            },
        );
        self.network.ban_peer(peer, DEFAULT_BAN_TIME, reason);
        self.verify_queue.write().await.remove_txs_by_peer(&peer);
```

**File:** tx-pool/src/process.rs (L720-720)
```rust
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

**File:** tx-pool/src/process.rs (L736-748)
```rust
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

**File:** util/types/src/core/tx_pool.rs (L92-92)
```rust
            Reject::DeclaredWrongCycles(..) => true,
```

**File:** sync/src/relayer/transactions_process.rs (L85-92)
```rust
                for (tx, declared_cycles) in txs {
                    if let Err(e) = tx_pool
                        .submit_remote_tx(tx.clone(), declared_cycles, peer)
                        .await
                    {
                        error!("submit_tx error {}", e);
                    }
                }
```

**File:** test/src/specs/tx_pool/declared_wrong_cycles.rs (L24-33)
```rust
        let tx = node0.new_transaction_spend_tip_cellbase();

        relay_tx(&net, node0, tx, ALWAYS_SUCCESS_SCRIPT_CYCLE + 1);

        let result = wait_until(5, || {
            let tx_pool_info = node0.get_tip_tx_pool_info();
            tx_pool_info.orphan.value() == 0 && tx_pool_info.pending.value() == 0
        });
        assert!(result, "Declared wrong cycles should be rejected");
    }
```
