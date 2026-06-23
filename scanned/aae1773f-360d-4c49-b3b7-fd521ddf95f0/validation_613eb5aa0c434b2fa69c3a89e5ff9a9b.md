### Title
Remote Peer Can DoS Local `send_transaction` Submission via Verify-Queue Occupation with Wrong Declared Cycles — (`tx-pool/src/process.rs`)

---

### Summary

The CKB tx-pool exposes two independent, uncoordinated paths for adding a transaction. A remote peer can occupy the verify queue for a target transaction by declaring a deliberately wrong cycle count. While the entry sits in the queue, every local `send_transaction` RPC call for the same transaction is rejected with `Duplicated`. Because `DeclaredWrongCycles` does not trigger a peer ban, the remote peer can immediately re-enqueue the transaction after it is rejected, sustaining the denial-of-service indefinitely.

---

### Finding Description

**Two independent, uncoordinated submission paths**

| Path | Entry point | Mechanism |
|---|---|---|
| Local (RPC user) | `send_transaction` → `submit_local_tx` → `process_tx(tx, None)` | Synchronous full verification, direct pool insertion |
| Remote (relay peer) | Relay protocol → `submit_remote_tx(tx, declared_cycles, peer)` → `resumeble_process_tx` → `enqueue_verify_queue` | Async; tx placed in verify queue with peer-supplied `declared_cycles` |

**Local path is blocked by verify-queue occupancy**

`process_tx` (used by `submit_local_tx`) checks the verify queue before doing any work:

```rust
// tx-pool/src/process.rs  ~line 409
if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
    return Err(Reject::Duplicated(tx.hash()));
}
```

If a remote peer has already placed the same transaction in the verify queue, the local user receives `Duplicated` immediately, with no opportunity to proceed.

**Wrong `declared_cycles` causes rejection without peer ban**

When the verify manager eventually dequeues and processes the entry, it compares the peer-supplied value against the actual result:

```rust
// tx-pool/src/process.rs  ~line 736-748
if let Some(declared) = declared_cycles
    && declared != verified.cycles
{
    return Some((
        Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
        snapshot,
    ));
}
```

`DeclaredWrongCycles` is not classified as a malformed transaction, so the peer-ban path in `non_contextual_verify` is never reached:

```rust
// tx-pool/src/process.rs  ~line 323-331
if reject.is_malformed_tx()
    && let Some(remote) = remote
{
    self.ban_malformed(remote.1, format!("reject {reject}")).await;
}
```

The peer is not banned, not rate-limited, and the transaction is not added to any recent-reject filter that would block re-enqueue. The remote peer can immediately call `submit_remote_tx` again with the same wrong `declared_cycles`.

**Verify-queue deduplication does not protect the local user**

`VerifyQueue::add_tx` silently returns `Ok(false)` for a duplicate non-proposal entry, so the remote peer's re-submission attempt while the tx is still in the queue is harmlessly dropped. But once the verify manager pops and rejects the entry, the slot is free and the remote peer can re-occupy it before the local user retries.

```rust
// tx-pool/src/component/verify_queue.rs  ~line 204-209
if self.contains_key(&tx.proposal_short_id()) {
    if is_proposal_tx {
        self.remove_tx(&tx.proposal_short_id());
    } else {
        return Ok(false);   // silent no-op for remote re-submission while occupied
    }
}
```

**`declared_cycles` also controls queue priority**

A peer that declares a very large cycle count causes `is_large_cycle = true`, deprioritising the entry in the verify queue and extending the window during which the local user is blocked:

```rust
// tx-pool/src/component/verify_queue.rs  ~line 212-214
let is_large_cycle = remote
    .map(|(cycles, _)| cycles > self.large_cycle_threshold)
    .unwrap_or(false);
```

---

### Impact Explanation

Any remote relay peer that learns the hash of a transaction a local user intends to submit can:

1. Enqueue the transaction with a wrong `declared_cycles` value.
2. Keep the local user's `send_transaction` RPC calls failing with `PoolRejectedDuplicatedTransaction (-1107)`.
3. Re-enqueue immediately after each rejection, sustaining the block indefinitely.

The local user cannot self-correct: there is no API to remove a transaction from the verify queue, and the `send_transaction` path has no fallback that bypasses the queue check.

---

### Likelihood Explanation

- Any unprivileged relay peer can submit transactions via the standard relay protocol.
- The attacker only needs to know the transaction hash (observable from mempool gossip or a dApp broadcast).
- No cryptographic material or privileged access is required.
- The attack loop is cheap: submit with wrong cycles → wait for rejection → re-submit.
- No ban, no rate-limit, no recent-reject guard prevents the loop.

---

### Recommendation

1. **Ban or score-penalise peers on `DeclaredWrongCycles`**: treat it similarly to a malformed transaction for the purpose of peer scoring.
2. **Add the transaction to the recent-reject pool on `DeclaredWrongCycles`**: this prevents the same peer from immediately re-enqueuing the same transaction.
3. **Allow local `submit_local_tx` to bypass the verify-queue occupancy check** (or give it priority over remote entries): local RPC submissions are authenticated by the node operator and should not be blocked by unauthenticated remote peers.

---

### Proof of Concept

```
1. Local user constructs tx T (actual cycles = 1_000_000).
2. Remote peer calls submit_remote_tx(T, declared_cycles=1, peer_id).
   → T is enqueued in verify_queue with is_large_cycle=false.
3. Local user calls send_transaction(T) via RPC.
   → process_tx checks verify_queue_contains(T) → true
   → returns Err(Reject::Duplicated(T.hash()))
   → RPC returns -1107 PoolRejectedDuplicatedTransaction.
4. Verify manager pops T, verifies actual cycles = 1_000_000 ≠ declared 1.
   → DeclaredWrongCycles error; T removed from queue; peer NOT banned.
5. Remote peer immediately calls submit_remote_tx(T, declared_cycles=1, peer_id) again.
   → T re-enqueued.
6. Repeat from step 3 indefinitely.
```

The local user's `send_transaction` calls fail permanently as long as the remote peer sustains the loop. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/process.rs (L318-333)
```rust
    pub(crate) async fn non_contextual_verify(
        &self,
        tx: &TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<(), Reject> {
        if let Err(reject) = non_contextual_verify(&self.consensus, tx) {
            if reject.is_malformed_tx()
                && let Some(remote) = remote
            {
                self.ban_malformed(remote.1, format!("reject {reject}"))
                    .await;
            }
            return Err(reject);
        }
        Ok(())
    }
```

**File:** tx-pool/src/process.rs (L335-353)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
    }
```

**File:** tx-pool/src/process.rs (L371-379)
```rust
    pub(crate) async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer)))
            .await
    }
```

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

**File:** tx-pool/src/process.rs (L736-749)
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
        }
```

**File:** tx-pool/src/component/verify_queue.rs (L198-236)
```rust
    pub fn add_tx(
        &mut self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        if self.contains_key(&tx.proposal_short_id()) {
            if is_proposal_tx {
                self.remove_tx(&tx.proposal_short_id());
            } else {
                return Ok(false);
            }
        }
        let tx_size = tx.data().serialized_size_in_block();
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "verify_queue total_tx_size overflowed, failed to add tx: {:#x}",
                tx.hash()
            ))
        })?;
        self.inner.insert(VerifyEntry {
            id: tx.proposal_short_id(),
            added_time: unix_time_as_millis(),
            inner: Entry { tx, remote },
            is_large_cycle,
            is_proposal_tx,
        });
        self.total_tx_size = total_tx_size;
        self.ready_rx.notify_one();
        Ok(true)
```

**File:** tx-pool/src/service.rs (L260-285)
```rust
    /// Submit local tx to tx-pool
    pub fn submit_local_tx(&self, tx: TransactionView) -> Result<SubmitTxResult, AnyError> {
        send_message!(self, SubmitLocalTx, tx)
    }

    /// test if a tx can be accepted by tx-pool
    /// Won't be broadcasted to network
    /// won't be insert to tx-pool
    pub fn test_accept_tx(&self, tx: TransactionView) -> Result<TestAcceptTxResult, AnyError> {
        send_message!(self, TestAcceptTx, tx)
    }

    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
    }

    /// Submit remote tx with declared cycles and origin to tx-pool
    pub async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<(), AnyError> {
        send_message!(self, SubmitRemoteTx, (tx, declared_cycles, peer))
    }
```

**File:** tx-pool/src/service.rs (L805-825)
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
        Message::SubmitLocalTestTx(Request {
            responder,
            arguments: tx,
        }) => {
            let result = service
                .resumeble_process_tx(tx, false, None)
                .await
                .map(|_| ());
            if let Err(e) = responder.send(result) {
                error!("Responder sending submit_tx result failed {:?}", e);
            };
        }
```
