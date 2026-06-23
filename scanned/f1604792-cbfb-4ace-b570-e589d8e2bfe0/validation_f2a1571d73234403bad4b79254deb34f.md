### Title
`catch_unwind` in `ConsumeUnverifiedBlocks::start` Silently Drops `verify_callback` on Panic, Bypassing Peer Punishment - (`chain/src/verify.rs`)

---

### Summary

In `chain/src/verify.rs`, the `ConsumeUnverifiedBlocks::start` loop wraps each call to `consume_unverified_blocks` in a `catch_unwind`. When a panic occurs, the handler logs the error and removes the block from `is_pending_verify`, but **never invokes the `verify_callback`**. The callback — which is responsible for banning the peer that sent an invalid block — is silently dropped. This is the direct CKB analog to the Distributor contract's asymmetric `try/catch` error handling: one error path (normal return) correctly fires the callback, while the other (panic) silently swallows it.

---

### Finding Description

In `ConsumeUnverifiedBlocks::start` (`chain/src/verify.rs`, lines 86–96):

```rust
if let Err(payload) = catch_unwind(AssertUnwindSafe(|| {
    self.processor.consume_unverified_blocks(unverified_task);
})) {
    error!(
        "consume unverified block {}-{} panicked: {}",
        block_number, block_hash,
        panic_payload_to_string(payload.as_ref())
    );
    self.processor.is_pending_verify.remove(&block_hash);
    // ← verify_callback is NEVER called here
}
```

The `unverified_task` (of type `UnverifiedBlock`) is moved into the closure. It carries an `Option<VerifyCallback>` field. In the normal (non-panic) path, `consume_unverified_blocks` explicitly calls the callback at lines 195–197:

```rust
if let Some(callback) = verify_callback {
    callback(verify_result);
}
```

But when the function panics — for any reason — `unverified_task` is dropped inside the closure without the callback ever being invoked. The `catch_unwind` handler has no recovery path that calls the callback.

The `verify_callback` is registered by two callers:

1. **`BlockProcess::execute` (sync, `sync/src/synchronizer/block_process.rs` lines 44–70)** — the callback calls `post_sync_process` → `nc.ban_peer(peer_id, ...)` when the block is invalid. If the callback is dropped, the peer is never punished.

2. **`Relayer::accept_block` (`sync/src/relayer/mod.rs` lines 291–334)** — the callback calls `post_sync_process` → `nc.ban_peer(peer_id, ...)` on invalid blocks, and `build_and_broadcast_compact_block` on valid ones. Both branches are silently skipped.

3. **`blocking_process_block_internal` (`chain/src/chain_controller.rs` lines 86–108)** — the callback sends through a oneshot channel. If dropped, `recv()` returns `Err(RecvError)`, which is handled by `unwrap_or_else` — this case is tolerable but still incorrect.

Panic-triggering paths inside `consume_unverified_blocks` include:

- `self.shared.store().get_tip_header().expect("tip_header must exist")` (line ~157) — panics if the DB tip is missing after a failed write
- `self.shared.store().get_block_ext(&tip.hash()).expect("tip header's ext must exist")` (line ~163) — panics if the ext is absent
- `self.shared.consensus().next_epoch_ext(...).expect("epoch should be stored")` (line ~314) — panics if epoch data is missing
- Any panic propagated from the contextual verifier or script executor

---

### Impact Explanation

When a panic occurs during block verification:

1. **Peer punishment is silently bypassed.** The peer that submitted the invalid block is never banned. It can continue sending blocks — including the same invalid block — indefinitely without consequence. This undermines the protocol's Sybil-resistance and anti-spam mechanisms.

2. **Block status may be left inconsistent.** If the panic occurs after `verify_block` returns `Err` but before `insert_block_status(BLOCK_INVALID)` completes, the block is not marked invalid. Subsequent re-submissions of the same block will re-enter the full verification pipeline.

3. **`submit_block` RPC caller receives a misleading error.** For the blocking path, the caller gets a channel-receive error rather than the actual verification failure, making it impossible to distinguish a DB failure from a consensus rejection.

---

### Likelihood Explanation

The `catch_unwind` was deliberately added to prevent the verification thread from crashing, which implies the developers are aware that panics can occur. Panic-triggering conditions include:

- **DB inconsistency after a partial write** (e.g., disk full, RocksDB write error) — the `expect` calls on `get_tip_header` and `get_block_ext` will panic
- **Epoch data missing** — `next_epoch_ext(...).expect(...)` panics if epoch storage is incomplete
- **Any future regression** in the verification pipeline that introduces an `unwrap`/`expect`

An unprivileged P2P peer triggers this path simply by sending a `SendBlock` message. The peer does not need to know the exact panic condition — any block that reaches `consume_unverified_blocks` and causes a panic (even due to a transient DB error) will result in the peer escaping punishment.

---

### Recommendation

In the `catch_unwind` panic handler, reconstruct and fire the callback with an error result before discarding the block. Since `unverified_task` is moved into the closure, the callback must be extracted before the closure is entered, or the panic payload must be inspected to recover it. A minimal fix:

```rust
// Extract callback before moving unverified_task into the closure
let verify_callback = unverified_task.verify_callback.take();
let block_hash_for_cb = block_hash.clone();

if let Err(payload) = catch_unwind(AssertUnwindSafe(|| {
    self.processor.consume_unverified_blocks(unverified_task);
})) {
    error!("consume unverified block {}-{} panicked: {}", ...);
    self.processor.is_pending_verify.remove(&block_hash);

    // Fire the callback with an error so the peer is still punished
    if let Some(cb) = verify_callback {
        cb(Err(InternalErrorKind::System
            .other("block verification panicked")
            .into()));
    }
}
```

---

### Proof of Concept

1. A remote peer connects and sends a `SendBlock` P2P message containing a valid-looking block (passes non-contextual checks).
2. `BlockProcess::execute` registers a `verify_callback` that calls `nc.ban_peer(peer_id, ...)` on error.
3. The block enters `ConsumeUnverifiedBlocks`. During `consume_unverified_blocks`, a DB `expect` panics (e.g., `get_tip_header()` returns `None` due to a prior write failure).
4. `catch_unwind` catches the panic. The handler removes the block from `is_pending_verify` but does **not** call the callback.
5. The peer is never banned. It reconnects and repeats, causing repeated verification panics with no consequence.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** chain/src/verify.rs (L86-96)
```rust
                        if let Err(payload) = catch_unwind(AssertUnwindSafe(|| {
                            self.processor.consume_unverified_blocks(unverified_task);
                        })) {
                            error!(
                                "consume unverified block {}-{} panicked: {}",
                                block_number,
                                block_hash,
                                panic_payload_to_string(payload.as_ref())
                            );
                            self.processor.is_pending_verify.remove(&block_hash);
                        }
```

**File:** chain/src/verify.rs (L193-197)
```rust
        self.is_pending_verify.remove(&block_hash);

        if let Some(callback) = verify_callback {
            callback(verify_result);
        }
```

**File:** sync/src/synchronizer/block_process.rs (L44-70)
```rust
            let verify_callback = {
                let nc: Arc<dyn CKBProtocolContext + Sync> = Arc::clone(&self.nc);
                let peer_id: PeerIndex = self.peer;
                let block_hash: Byte32 = block.hash();
                Box::new(move |verify_result: Result<bool, ckb_error::Error>| {
                    match verify_result {
                        Ok(_) => {}
                        Err(err) => {
                            let is_internal_db_error = is_internal_db_error(&err);
                            if is_internal_db_error {
                                return;
                            }

                            // punish the malicious peer
                            post_sync_process(
                                nc.as_ref(),
                                peer_id,
                                "SendBlock",
                                StatusCode::BlockIsInvalid.with_context(format!(
                                    "block {} is invalid, reason: {}",
                                    block_hash, err
                                )),
                            );
                        }
                    };
                })
            };
```

**File:** sync/src/relayer/mod.rs (L291-334)
```rust
        let verify_callback = {
            let nc: Arc<dyn CKBProtocolContext + Sync> = Arc::clone(&nc);
            let block = Arc::clone(&block);
            let shared = Arc::clone(self.shared());
            let msg_name = msg_name.to_owned();
            Box::new(move |result: VerifyResult| match result {
                Ok(verified) => {
                    if !verified {
                        debug!(
                            "block {}-{} has verified already, won't build compact block and broadcast it",
                            block.number(),
                            block.hash()
                        );
                        return;
                    }

                    build_and_broadcast_compact_block(nc.as_ref(), shared.shared(), peer_id, block);
                }
                Err(err) => {
                    error!(
                        "verify block {}-{} failed: {:?}, won't build compact block and broadcast it",
                        block.number(),
                        block.hash(),
                        err
                    );

                    let is_internal_db_error = is_internal_db_error(&err);
                    if is_internal_db_error {
                        return;
                    }

                    // punish the malicious peer
                    post_sync_process(
                        nc.as_ref(),
                        peer_id,
                        &msg_name,
                        StatusCode::BlockIsInvalid.with_context(format!(
                            "block {} is invalid, reason: {}",
                            block.hash(),
                            err
                        )),
                    );
                }
            })
```

**File:** chain/src/chain_controller.rs (L84-108)
```rust
        let (verify_result_tx, verify_result_rx) = ckb_channel::oneshot::channel::<VerifyResult>();

        let verify_callback = {
            move |result: VerifyResult| {
                if let Err(err) = verify_result_tx.send(result) {
                    error!(
                        "blocking send verify_result failed: {}, this shouldn't happen",
                        err
                    )
                }
            }
        };

        let lonely_block = LonelyBlock {
            block,
            switch,
            verify_callback: Some(Box::new(verify_callback)),
        };

        self.asynchronous_process_lonely_block(lonely_block);
        verify_result_rx.recv().unwrap_or_else(|err| {
            Err(InternalErrorKind::System
                .other(format!("blocking recv verify_result failed: {}", err))
                .into())
        })
```

**File:** chain/src/lib.rs (L44-45)
```rust
/// VerifyCallback is the callback type to be called after block verification
pub type VerifyCallback = Box<dyn FnOnce(VerifyResult) + Send + Sync>;
```
