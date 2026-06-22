## Analysis

I have the full source of `block_proposal_process.rs` and can trace the exact code path. Let me evaluate each claim.

---

### Code Path Trace

**Step 1 – Count-only guard (lines 27-36)** [1](#0-0) 

The only size check validates transaction *count* against `max_block_proposals_limit * max_uncles_num`. There is no check on total byte size of the transactions.

**Step 2 – Full deserialization before inflight filter (lines 39-45)** [2](#0-1) 

`.to_entity().into_view()` allocates owned `TransactionView` objects for **every** transaction in the message. This happens *before* the inflight filter at lines 55-62. Even if the filter later discards some, all are already allocated in memory.

**Step 3 – Inflight filter (lines 55-62)** [3](#0-2) 

`remove_inflight_proposals` only passes transactions whose proposal short IDs were previously requested via `GetBlockProposal`. This is the guard the question claims can be bypassed — but it cannot be bypassed arbitrarily. The attacker must have first caused the victim to emit a `GetBlockProposal` for those IDs.

**Step 4 – notify_txs_async (line 69)** [4](#0-3) 

The filtered `asked_txs` are sent to the tx-pool channel.

---

### Evaluating the Precondition

The question's precondition is: *"the attacker first sends a compact block to trigger GetBlockProposal for 3000 proposal IDs."*

This is **achievable** by an unprivileged peer:
- The attacker connects as a normal P2P peer.
- Sends a compact block containing up to `max_block_proposals_limit * max_uncles_num` proposal short IDs that the victim doesn't have in its pool.
- The victim responds with `GetBlockProposal` for those IDs.
- The attacker then responds with `BlockProposal` containing 3000 large transactions.

All 3000 transactions pass `remove_inflight_proposals` because they were all requested. The inflight filter is **not** a defense here — it is satisfied by design.

---

### The Missing Bound

The count limit (`max_block_proposals_limit * max_uncles_num`) caps the number of transactions but **not their aggregate byte size**. Each transaction can be up to `TRANSACTION_SIZE_LIMIT` (512 KB per the consensus). The product is:

```
3000 transactions × 512 KB = ~1.5 GB
```

This entire payload is deserialized into `Vec<core::TransactionView>` in a single synchronous `.collect()` before any size-based rejection occurs.

I searched for P2P-level frame/message size limits (`MAX_FRAME_LENGTH`, `LengthDelimited`, `max_length`, `frame_size`, `message_size`) and found **no matches** in the codebase. There is no network-layer cap that would prevent a 1.5 GB `BlockProposal` message from being received and processed.

---

### Verdict

The vulnerability is **real and reachable**:

- **Entrypoint**: Unprivileged P2P peer, two-message sequence (compact block → BlockProposal).
- **Missing guard**: Byte-size bound on `BlockProposal` transactions; count-only check at lines 27-36.
- **Deserialization before filtering**: All transactions allocated before inflight check.
- **Impact**: A single `BlockProposal` message can force the node to allocate up to ~1.5 GB of `TransactionView` objects, causing memory exhaustion and potential OOM crash or severe degradation.

---

### Title
Unbounded Memory Allocation via Oversized `BlockProposal` Message — (`sync/src/relayer/block_proposal_process.rs`)

### Summary
`BlockProposalProcess::execute` validates only the *count* of transactions in a `BlockProposal` message, not their aggregate byte size. An attacker who first triggers `GetBlockProposal` for `max_block_proposals_limit * max_uncles_num` (≈3000) proposal IDs can respond with 3000 transactions each near `TRANSACTION_SIZE_LIMIT` (512 KB), causing the victim node to deserialize and allocate up to ~1.5 GB of `TransactionView` objects in a single message handler invocation.

### Finding Description
In `block_proposal_process.rs`, the guard at lines 27-36 rejects messages where `transactions().len() > max_block_proposals_limit * max_uncles_num`, but imposes no limit on total serialized byte size. At lines 39-45, all transactions are unconditionally deserialized via `.to_entity().into_view()` and collected into a `Vec<core::TransactionView>` before the inflight filter at lines 55-62 is applied. The inflight filter is not a defense against this attack — it is fully satisfied when the attacker first sends a compact block to trigger `GetBlockProposal` for the same proposal IDs. No P2P-layer message size cap was found in the codebase. [1](#0-0) [2](#0-1) 

### Impact Explanation
A single two-message exchange (compact block + BlockProposal) from one unprivileged peer can force the victim node to allocate up to ~1.5 GB of heap memory. On memory-constrained nodes this causes OOM termination; on well-provisioned nodes it causes severe GC/allocator pressure, stalling block processing and tx-pool operations, effectively congesting the node at negligible cost to the attacker (no PoW, no fees, no stake).

### Likelihood Explanation
The attack requires only a standard P2P connection and two crafted messages. No privileged access, leaked keys, or majority hashpower is needed. The two-step setup (compact block → BlockProposal) is straightforward to implement and can be repeated continuously from multiple peers.

### Recommendation
Add a byte-size bound check immediately after the count check:

```rust
let total_size: usize = block_proposals.transactions().iter()
    .map(|tx| tx.as_slice().len())
    .sum();
if total_size > MAX_BLOCK_PROPOSAL_TOTAL_BYTES {
    return StatusCode::ProtocolMessageIsMalformed.with_context(...);
}
```

Alternatively, enforce `TRANSACTION_SIZE_LIMIT` per transaction during deserialization, or add a P2P-layer cap on `BlockProposal` message size.

### Proof of Concept
1. Connect to a victim node as a P2P peer.
2. Send a `CompactBlock` message containing 3000 novel proposal short IDs (transactions not in the victim's pool).
3. Victim responds with `GetBlockProposal` for those 3000 IDs.
4. Respond with a `BlockProposal` containing 3000 transactions, each padded to ~512 KB (e.g., large witness data).
5. Observe victim node memory usage spike by ~1.5 GB during `BlockProposalProcess::execute`.
6. Repeat to sustain memory pressure or trigger OOM.

### Citations

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

**File:** sync/src/relayer/block_proposal_process.rs (L39-45)
```rust
        let unknown_txs: Vec<core::TransactionView> = self
            .message
            .transactions()
            .iter()
            .map(|x| x.to_entity().into_view())
            .filter(|tx| !sync_state.already_known_tx(&tx.hash()))
            .collect();
```

**File:** sync/src/relayer/block_proposal_process.rs (L55-62)
```rust
        let removes = sync_state.remove_inflight_proposals(&proposals);
        let mut asked_txs = Vec::new();
        for (previously_in, tx) in removes.into_iter().zip(unknown_txs) {
            if previously_in {
                sync_state.mark_as_known_tx(tx.hash());
                asked_txs.push(tx);
            }
        }
```

**File:** sync/src/relayer/block_proposal_process.rs (L68-75)
```rust
        let tx_pool = self.relayer.shared.shared().tx_pool_controller();
        if let Err(err) = tx_pool.notify_txs_async(asked_txs).await {
            warn_target!(
                crate::LOG_TARGET_RELAY,
                "BlockProposal notify_txs error: {:?}",
                err,
            );
        }
```
