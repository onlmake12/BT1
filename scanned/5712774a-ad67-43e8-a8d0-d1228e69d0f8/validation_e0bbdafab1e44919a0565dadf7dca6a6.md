### Title
Unbounded Relay Amplification via Duplicate RPC Transaction Submission — (`tx-pool/src/process.rs`)

---

### Summary

The `after_process` function in `tx-pool/src/process.rs` contains an explicit branch that fires `send_result_to_relayer(TxVerificationResult::Ok { original_peer: None, tx_hash })` whenever a local RPC submission results in `Reject::Duplicated`. There is no rate-limiting, deduplication, or submission counter on this path. An attacker with RPC access can submit the same already-pooled transaction N times and generate exactly N `TxVerificationResult::Ok` relay events, each of which the relayer broadcasts to every connected peer.

---

### Finding Description

**Root cause — `after_process`, lines 538–545:**

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

This branch is reached via the following concrete path:

1. **RPC entry**: `send_transaction` dispatches `Message::SubmitLocalTx` → `process_tx(tx, None)`. [2](#0-1) 

2. **`process_tx`** skips the early-return guard (`verify_queue_contains` / `orphan_contains`) because the tx is already in the pool, not the verify queue or orphan pool. [3](#0-2) 

3. **`_process_tx` → `pre_check` → `check_txid_collision`** detects the duplicate and returns `Err(Reject::Duplicated(...))` via `try_or_return_with_snapshot!`. [4](#0-3) 

4. **`after_process`** is called with `remote = None` and `ret = Err(Reject::Duplicated(...))`, hitting the branch above. [5](#0-4) 

**Relay amplification — `send_bulk_of_tx_hashes`, lines 662–669:**

When `original_peer = None`, the relayer pushes the `tx_hash` into `selected` for **every connected peer** with no prior deduplication check. `mark_as_known_tx` is called after the push, but it does not prevent the same hash from being pushed again on the next duplicate submission. [6](#0-5) 

The `tx_relay_sender` is a `crossbeam_channel` (unbounded or large-bounded). `send_result_to_relayer` is non-blocking on the happy path and only logs an error on failure — there is no back-pressure that would throttle the attacker. [7](#0-6) 

---

### Impact Explanation

Each duplicate RPC call costs the attacker only a cheap local RPC round-trip (no PoW, no fee, no new transaction required). Each call injects one `TxVerificationResult::Ok` into the relay channel. The relayer drains up to `MAX_RELAY_TXS_NUM_PER_BATCH` items per tick and sends a `RelayTransactionHashes` message to every full-relay peer for each item. Submitting the same tx 1000 times generates 1000 × P relay messages (P = number of connected peers), congesting the P2P relay channel and degrading throughput for honest peers.

---

### Likelihood Explanation

The RPC endpoint is the documented, standard interface for submitting transactions. Any operator who exposes the RPC (common for wallets, exchanges, and public nodes) is vulnerable. The attacker needs no special privilege, no key material, and no new valid transaction — only one transaction already accepted into the pool. The exploit is trivially scriptable.

---

### Recommendation

In the `Err(Reject::Duplicated(_))` branch for `remote = None`, suppress the relay broadcast entirely, or gate it behind a per-transaction cooldown (e.g., only re-broadcast if the last broadcast for this `tx_hash` was more than N seconds ago). The comment "re-broadcast tx when it's duplicated and submitted through local rpc" describes an intentional feature, but it must be rate-limited per `tx_hash`.

Additionally, in `send_bulk_of_tx_hashes`, check whether the `tx_hash` is already in the known-tx filter before adding it to `selected` for the `original_peer = None` case.

---

### Proof of Concept

```python
import requests, json

RPC = "http://127.0.0.1:8114"

# Step 1: submit a valid tx once (accepted into pool)
tx = { ... }  # any valid, fee-paying transaction
requests.post(RPC, json={"id":1,"jsonrpc":"2.0","method":"send_transaction","params":[tx,"passthrough"]})

# Step 2: submit the same tx 1000 more times
for i in range(1000):
    r = requests.post(RPC, json={"id":i+2,"jsonrpc":"2.0","method":"send_transaction","params":[tx,"passthrough"]})
    # Each call returns PoolRejectedDuplicatedTransaction (-1107)
    # but internally fires send_result_to_relayer(TxVerificationResult::Ok)

# Expected: relay channel receives 1001 Ok events → 1001 × P relay broadcasts
# Invariant violated: relay broadcasts must only be triggered by newly accepted transactions
```

### Citations

**File:** tx-pool/src/process.rs (L401-426)
```rust
    pub(crate) async fn process_tx(
        &self,
        tx: TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<Completed, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }

        if let Some((ret, snapshot)) = self
            ._process_tx(tx.clone(), remote.map(|r| r.0), None)
            .await
        {
            self.after_process(tx, remote, &snapshot, &ret).await;
            ret
        } else {
            // currently, the returned cycles is not been used, mock 0 if delay
            Ok(Completed {
                cycles: 0,
                fee: Capacity::zero(),
            })
        }
    }
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

**File:** tx-pool/src/process.rs (L673-677)
```rust
    pub(crate) fn send_result_to_relayer(&self, result: TxVerificationResult) {
        if let Err(e) = self.tx_relay_sender.send(result) {
            error!("tx-pool tx_relay_sender internal error {}", e);
        }
    }
```

**File:** tx-pool/src/service.rs (L805-813)
```rust
        Message::SubmitLocalTx(Request {
            responder,
            arguments: tx,
        }) => {
            let result = service.process_tx(tx, None).await.map(|_| ());
            if let Err(e) = responder.send(result) {
                error!("Responder sending submit_tx result failed {:?}", e);
            };
        }
```

**File:** tx-pool/src/util.rs (L20-26)
```rust
pub(crate) fn check_txid_collision(tx_pool: &TxPool, tx: &TransactionView) -> Result<(), Reject> {
    let short_id = tx.proposal_short_id();
    if tx_pool.contains_proposal_id(&short_id) {
        return Err(Reject::Duplicated(tx.hash()));
    }
    Ok(())
}
```

**File:** sync/src/relayer/mod.rs (L662-669)
```rust
                                None => {
                                    // since this tx is submitted through local rpc, it is assumed to be a new tx for all connected peers
                                    let hashes = selected
                                        .entry(*target)
                                        .or_insert_with(|| Vec::with_capacity(BUFFER_SIZE));
                                    hashes.push(tx_hash.clone());
                                    self.shared.state().mark_as_known_tx(tx_hash.clone());
                                }
```
