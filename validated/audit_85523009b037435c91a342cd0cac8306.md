### Title
`check_data()` Structural Validation Inconsistently Applied Across Relay Message Handlers — (`sync/src/relayer/mod.rs`)

---

### Summary

In `Relayer::try_process()`, the `check_data()` structural validation guard is applied to three relay message variants that carry transaction data (`CompactBlock`, `RelayTransactions`, `BlockTransactions`) but is entirely absent for `BlockProposal`, which also carries full `Transaction` objects. Any connected peer can send a `BlockProposal` message containing structurally malformed transactions — invalid `hash_type`, invalid `dep_type`, or mismatched `outputs`/`outputs_data` counts — that bypass the early rejection gate and are forwarded directly into the tx pool via `notify_txs_async`.

---

### Finding Description

`Relayer::try_process()` dispatches eight relay message variants. Three of them call `reader.check_data()` before constructing the process object:

```
CompactBlock      → check_data() ✅
RelayTransactions → check_data() ✅
BlockTransactions → check_data() ✅

RelayTransactionHashes → no check_data() (hash-only, no tx body)
GetRelayTransactions   → no check_data() (hash-only, no tx body)
GetBlockTransactions   → no check_data() (hash-only, no tx body)
GetBlockProposal       → no check_data() (proposal short IDs only)
BlockProposal          → no check_data() ❌  ← carries full Transaction objects
``` [1](#0-0) 

`check_data()` for a `Transaction` recursively verifies:
- `outputs().len() == outputs_data().len()`
- every `CellDep` has `dep_type ≤ 1`
- every `CellOutput` script has a valid `hash_type` byte [2](#0-1) 

`BlockProposalProcess::execute()` receives the raw reader, iterates its transactions with `.to_entity().into_view()`, filters for unknown ones, and passes them directly to `tx_pool.notify_txs_async(asked_txs)` — with no structural pre-check. [3](#0-2) 

The only guard present in `BlockProposalProcess` is a count limit on the number of transactions, not a structural validity check on each transaction's fields. [4](#0-3) 

By contrast, `BlockTransactionsProcess` — which also carries full `Transaction` objects — is gated behind `reader.check_data()` before any processing begins. [5](#0-4) 

---

### Impact Explanation

A malformed `BlockProposal` message (e.g., a transaction with `hash_type = 0xFF`, `dep_type = 0xFF`, or `outputs.len() ≠ outputs_data.len()`) passes the relay dispatcher without rejection and reaches `notify_txs_async`. Downstream tx-pool validation code that assumes structurally valid input — for example, any code that converts the raw `hash_type` byte to a `ScriptHashType` enum via an infallible `From` impl — can panic, crashing the node. Even if the tx pool handles the error gracefully, the missing early gate wastes tx-pool resources and violates the defense-in-depth contract that `check_data()` is supposed to enforce uniformly for all message types carrying transaction bodies.

The `check_data()` implementations for `BlockTransactionsReader` and `RelayTransactionsReader` exist precisely because transaction bodies require this guard; `BlockProposalReader` carries the same payload type and is missing the same guard. [6](#0-5) 

---

### Likelihood Explanation

Any peer connected on the `RelayV3` protocol can send a `BlockProposal` message at any time after the IBD check passes. No special privilege, key, or majority hashpower is required. The message is accepted by the outer `from_compatible_slice` parser (which only checks molecule framing, not field semantics), so the malformed payload reaches `try_process` without triggering the ban path. [7](#0-6) 

---

### Recommendation

Add a `check_data()` implementation for `BlockProposalReader` in `util/gen-types/src/extension/check_data.rs` (mirroring `BlockTransactionsReader::check_data`) and gate the `BlockProposal` arm in `Relayer::try_process()` identically to the `BlockTransactions` arm:

```rust
packed::RelayMessageUnionReader::BlockProposal(reader) => {
    if reader.check_data() {
        BlockProposalProcess::new(reader, self).execute().await
    } else {
        StatusCode::ProtocolMessageIsMalformed
            .with_context("BlockProposal is invalid")
    }
}
``` [8](#0-7) 

---

### Proof of Concept

1. Connect to a non-IBD CKB node on the `RelayV3` protocol.
2. Craft a `BlockProposal` molecule message containing one `Transaction` where `hash_type = 0xFF` (invalid) and `outputs.len() = 1` but `outputs_data.len() = 0` (mismatched).
3. Send the message. The outer `from_compatible_slice` parser accepts it (molecule framing is valid).
4. `try_process` dispatches to `BlockProposalProcess::execute()` without calling `check_data()`.
5. The malformed transaction passes the count-limit check, is converted via `.to_entity().into_view()`, and is submitted to `notify_txs_async`.
6. Any downstream code that performs an infallible `hash_type` byte-to-enum conversion panics, or the tx pool processes structurally invalid data it was never designed to receive. [9](#0-8) [10](#0-9)

### Citations

**File:** sync/src/relayer/mod.rs (L112-123)
```rust
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));

        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** sync/src/relayer/mod.rs (L125-174)
```rust
        match message {
            packed::RelayMessageUnionReader::CompactBlock(reader) => {
                if reader.check_data() {
                    CompactBlockProcess::new(reader, self, nc, peer)
                        .execute()
                        .await
                } else {
                    StatusCode::ProtocolMessageIsMalformed.with_context("CompactBlock is invalid")
                }
            }
            packed::RelayMessageUnionReader::RelayTransactions(reader) => {
                if reader.check_data() {
                    TransactionsProcess::new(reader, self, nc, peer).execute()
                } else {
                    StatusCode::ProtocolMessageIsMalformed
                        .with_context("RelayTransactions is invalid")
                }
            }
            packed::RelayMessageUnionReader::RelayTransactionHashes(reader) => {
                TransactionHashesProcess::new(reader, self, peer).execute()
            }
            packed::RelayMessageUnionReader::GetRelayTransactions(reader) => {
                GetTransactionsProcess::new(reader, self, nc, peer)
                    .execute()
                    .await
            }
            packed::RelayMessageUnionReader::GetBlockTransactions(reader) => {
                GetBlockTransactionsProcess::new(reader, self, nc, peer)
                    .execute()
                    .await
            }
            packed::RelayMessageUnionReader::BlockTransactions(reader) => {
                if reader.check_data() {
                    BlockTransactionsProcess::new(reader, self, nc, peer)
                        .execute()
                        .await
                } else {
                    StatusCode::ProtocolMessageIsMalformed
                        .with_context("BlockTransactions is invalid")
                }
            }
            packed::RelayMessageUnionReader::GetBlockProposal(reader) => {
                GetBlockProposalProcess::new(reader, self, nc, peer)
                    .execute()
                    .await
            }
            packed::RelayMessageUnionReader::BlockProposal(reader) => {
                BlockProposalProcess::new(reader, self).execute().await
            }
        }
```

**File:** sync/src/relayer/mod.rs (L809-879)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        data: Bytes,
    ) {
        // If self is in the IBD state, don't process any relayer message.
        if self.shared.active_chain().is_initial_block_download() {
            return;
        }

        let msg = match packed::RelayMessageReader::from_compatible_slice(&data) {
            Ok(msg) => {
                let item = msg.to_enum();
                if let packed::RelayMessageUnionReader::CompactBlock(ref reader) = item {
                    if reader.count_extra_fields() > 1 {
                        info_target!(
                            crate::LOG_TARGET_RELAY,
                            "Peer {} sends us a malformed message: \
                             too many fields in CompactBlock",
                            peer_index
                        );
                        nc.ban_peer(
                            peer_index,
                            BAD_MESSAGE_BAN_TIME,
                            String::from(
                                "send us a malformed message: \
                                 too many fields in CompactBlock",
                            ),
                        );
                        return;
                    } else {
                        item
                    }
                } else {
                    match packed::RelayMessageReader::from_slice(&data) {
                        Ok(msg) => msg.to_enum(),
                        _ => {
                            info_target!(
                                crate::LOG_TARGET_RELAY,
                                "Peer {} sends us a malformed message: \
                                 too many fields",
                                peer_index
                            );
                            nc.ban_peer(
                                peer_index,
                                BAD_MESSAGE_BAN_TIME,
                                String::from(
                                    "send us a malformed message \
                                     too many fields",
                                ),
                            );
                            return;
                        }
                    }
                }
            }
            _ => {
                info_target!(
                    crate::LOG_TARGET_RELAY,
                    "Peer {} sends us a malformed message",
                    peer_index
                );
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };
```

**File:** util/gen-types/src/extension/check_data.rs (L8-13)
```rust
const MAX_DEP_TYPE: u8 = 1;

impl<'r> packed::ScriptReader<'r> {
    fn check_data(&self) -> bool {
        core::ScriptHashType::verify_value(self.hash_type().into())
    }
```

**File:** util/gen-types/src/extension/check_data.rs (L48-60)
```rust
impl<'r> packed::RawTransactionReader<'r> {
    fn check_data(&self) -> bool {
        self.outputs().len() == self.outputs_data().len()
            && self.cell_deps().check_data()
            && self.outputs().check_data()
    }
}

impl<'r> packed::TransactionReader<'r> {
    pub(crate) fn check_data(&self) -> bool {
        self.raw().check_data()
    }
}
```

**File:** util/gen-types/src/extension/check_data.rs (L97-121)
```rust
impl<'r> packed::BlockTransactionsReader<'r> {
    /// Recursively checks whether the structure of the binary data is correct.
    pub fn check_data(&self) -> bool {
        self.transactions().check_data()
    }
}

impl<'r> packed::RelayTransactionReader<'r> {
    fn check_data(&self) -> bool {
        self.transaction().check_data()
    }
}

impl<'r> packed::RelayTransactionVecReader<'r> {
    fn check_data(&self) -> bool {
        self.iter().all(|i| i.check_data())
    }
}

impl<'r> packed::RelayTransactionsReader<'r> {
    /// Recursively checks whether the structure of the binary data is correct.
    pub fn check_data(&self) -> bool {
        self.transactions().check_data()
    }
}
```

**File:** sync/src/relayer/block_proposal_process.rs (L27-36)
```rust
            let limit = shared.consensus().max_block_proposals_limit()
                * (shared.consensus().max_uncles_num() as u64);
            if (block_proposals.transactions().len() as u64) > limit {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "Transactions count({}) > consensus max_block_proposals_limit({}) * max_uncles_num({})",
                    block_proposals.transactions().len(),
                    shared.consensus().max_block_proposals_limit(),
                    shared.consensus().max_uncles_num(),
                ));
            }
```

**File:** sync/src/relayer/block_proposal_process.rs (L39-69)
```rust
        let unknown_txs: Vec<core::TransactionView> = self
            .message
            .transactions()
            .iter()
            .map(|x| x.to_entity().into_view())
            .filter(|tx| !sync_state.already_known_tx(&tx.hash()))
            .collect();

        if unknown_txs.is_empty() {
            return Status::ignored();
        }

        let proposals: Vec<packed::ProposalShortId> = unknown_txs
            .iter()
            .map(|tx| packed::ProposalShortId::from_tx_hash(&tx.hash()))
            .collect();
        let removes = sync_state.remove_inflight_proposals(&proposals);
        let mut asked_txs = Vec::new();
        for (previously_in, tx) in removes.into_iter().zip(unknown_txs) {
            if previously_in {
                sync_state.mark_as_known_tx(tx.hash());
                asked_txs.push(tx);
            }
        }

        if asked_txs.is_empty() {
            return Status::ignored();
        }

        let tx_pool = self.relayer.shared.shared().tx_pool_controller();
        if let Err(err) = tx_pool.notify_txs_async(asked_txs).await {
```
