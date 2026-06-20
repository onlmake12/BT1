### Title
Missing `check_data()` Validation for `BlockProposal` Relay Message Allows Malformed Transactions to Bypass Relay-Layer Integrity Gate — (`sync/src/relayer/mod.rs`)

---

### Summary

The relay message dispatcher in `sync/src/relayer/mod.rs` inconsistently applies `check_data()` validation before processing incoming P2P relay messages. Three message types that carry transaction data — `CompactBlock`, `RelayTransactions`, and `BlockTransactions` — are all guarded by `check_data()` before dispatch. However, `BlockProposal`, which also carries a `TransactionVec`, is dispatched directly to `BlockProposalProcess` with no `check_data()` call. An unprivileged P2P peer can exploit this gap to inject structurally malformed transactions (invalid `hash_type` enum values, invalid `dep_type` enum values, or mismatched `outputs`/`outputs_data` lengths) past the relay-layer validation gate.

---

### Finding Description

CKB defines a `check_data()` method on molecule reader types in `util/gen-types/src/extension/check_data.rs` to validate the semantic integrity of binary-encoded data structures before they are processed. For transactions, `RawTransactionReader::check_data()` enforces:

- `outputs.len() == outputs_data.len()`
- All `CellDep.dep_type` values are within the valid enum range (`<= MAX_DEP_TYPE = 1`)
- All `Script.hash_type` values are within the valid enum range (not `3`, which is reserved/invalid) [1](#0-0) 

In the relay message dispatcher, `check_data()` is called for `CompactBlock`, `RelayTransactions`, and `BlockTransactions` before their respective process handlers are invoked: [2](#0-1) 

However, `BlockProposal` — which carries `transactions: TransactionVec` per the molecule schema — is dispatched directly without any `check_data()` guard:

```rust
packed::RelayMessageUnionReader::BlockProposal(reader) => {
    BlockProposalProcess::new(reader, self).execute().await
}
``` [3](#0-2) 

The `BlockProposal` message schema confirms it carries a full `TransactionVec`: [4](#0-3) 

The `check_data()` implementations for `TransactionVecReader` and its sub-types are public and well-defined, but are simply never invoked for this message type: [5](#0-4) 

---

### Impact Explanation

A `BlockProposal` message is sent by a peer in response to a `GetBlockProposal` request, but any connected peer can send an unsolicited `BlockProposal` at any time. The `BlockProposalProcess` receives the raw transactions from this message and uses them in the block reconstruction pipeline (filling in proposal-zone transactions for compact block assembly). Without `check_data()`, a peer can inject transactions with:

- **Invalid `hash_type`** (e.g., value `3`, which is reserved and will cause script execution errors)
- **Invalid `dep_type`** (e.g., value `2`, which is out of range)
- **Mismatched `outputs`/`outputs_data` lengths** (structural inconsistency)

These malformed transactions bypass the relay-layer integrity gate that is consistently applied to all other transaction-bearing relay messages. They can enter the node's internal processing pipeline (proposal cache, tx pool submission path) without the same early rejection that `RelayTransactions` and `BlockTransactions` receive.

The inconsistency is directly analogous to the reported pattern: validation routines exist and are applied at some entry points but are silently omitted at others, creating an exploitable gap in the validation surface.

---

### Likelihood Explanation

Any unprivileged connected peer can send a `BlockProposal` message at any time — no authentication, no prior state, and no special role is required. The relay protocol does not restrict which peers may send `BlockProposal`. The rate limiter in `Relayer::try_process` applies to all relay messages except `CompactBlock`, so `BlockProposal` is rate-limited, but the rate limit (30 req/s) still allows a sustained stream of malformed messages. The attack requires only a TCP connection to the node's P2P port.

---

### Recommendation

Add a `check_data()` guard for `BlockProposal` in `sync/src/relayer/mod.rs`, consistent with the pattern already applied to `CompactBlock`, `RelayTransactions`, and `BlockTransactions`:

```rust
packed::RelayMessageUnionReader::BlockProposal(reader) => {
    if reader.check_data() {
        BlockProposalProcess::new(reader, self).execute().await
    } else {
        StatusCode::ProtocolMessageIsMalformed
            .with_context("BlockProposal is invalid")
    }
}
```

Additionally, implement `BlockProposalReader::check_data()` in `util/gen-types/src/extension/check_data.rs` delegating to `TransactionVecReader::check_data()`, mirroring the pattern used for `BlockTransactionsReader`: [6](#0-5) 

---

### Proof of Concept

1. Connect to a CKB node's P2P port as an unprivileged peer.
2. Craft a `RelayMessage` with union tag `7` (`BlockProposal`) containing a `TransactionVec` where one transaction has a `Script` with `hash_type = 3` (invalid enum value) or has `outputs.len() != outputs_data.len()`.
3. Send the message. The node's `Relayer::try_process` dispatches it directly to `BlockProposalProcess::execute()` without calling `check_data()`.
4. Observe that the same malformed transaction sent as `RelayTransactions` (union tag `1`) is rejected immediately with `ProtocolMessageIsMalformed` because `RelayTransactionsReader::check_data()` is called at line 136.

The inconsistency is confirmed by comparing the two dispatch arms in `sync/src/relayer/mod.rs` lines 135–141 (guarded) versus lines 171–173 (unguarded). [7](#0-6)

### Citations

**File:** util/gen-types/src/extension/check_data.rs (L36-66)
```rust
impl<'r> packed::CellDepReader<'r> {
    fn check_data(&self) -> bool {
        MAX_DEP_TYPE >= self.dep_type().into()
    }
}

impl<'r> packed::CellDepVecReader<'r> {
    fn check_data(&self) -> bool {
        self.iter().all(|i| i.check_data())
    }
}

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

impl<'r> packed::TransactionVecReader<'r> {
    fn check_data(&self) -> bool {
        self.iter().all(|i| i.check_data())
    }
}
```

**File:** util/gen-types/src/extension/check_data.rs (L97-102)
```rust
impl<'r> packed::BlockTransactionsReader<'r> {
    /// Recursively checks whether the structure of the binary data is correct.
    pub fn check_data(&self) -> bool {
        self.transactions().check_data()
    }
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

**File:** util/gen-types/schemas/extensions.mol (L190-192)
```text
table BlockProposal {
    transactions:               TransactionVec,
}
```
