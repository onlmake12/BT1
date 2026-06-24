Audit Report

## Title
Unsolicited `SendBlock` Accepted Without Sender Verification, Consuming Inflight Slot and Stalling Sync — (File: `sync/src/synchronizer/block_process.rs`)

## Summary
`BlockProcess::execute` calls `shared.new_block_received(&block)` without verifying that the sending peer matches the peer originally assigned to fetch that block. `new_block_received` removes the inflight entry keyed solely by block hash, allowing any connected peer to race-send a `SendBlock` for any in-flight hash, consume the inflight slot, and substitute attacker-controlled block data. After verification failure the block is marked `BLOCK_RECEIVED` (and subsequently `BLOCK_INVALID`), and the `BlockFetcher` skips blocks already carrying `BLOCK_RECEIVED`, stalling the node at that height.

## Finding Description

**Root cause — no peer check before consuming the inflight slot:**

`BlockProcess::execute` passes the block directly to `new_block_received` with no comparison between `self.peer` and the peer recorded in `inflight_states`: [1](#0-0) 

`new_block_received` calls `remove_by_block` keyed only on `(block.number(), block.hash())`: [2](#0-1) 

`remove_by_block` removes the `inflight_states` entry and applies the timing credit to `state.peer` (the originally assigned peer), not the actual sender: [3](#0-2) 

After `remove_by_block` returns `true`, `new_block_received` inserts `BLOCK_RECEIVED` into the `block_status_map` via a `Vacant`-only entry: [4](#0-3) 

**Why the stall persists:**

`BlockFetcher` explicitly skips any block whose status contains `BLOCK_RECEIVED`: [5](#0-4) 

Once the attacker's block fails verification and the status transitions to `BLOCK_INVALID`, the `block_status_map` entry is `Occupied`, so a subsequent `new_block_received` call for the same hash returns `false` at the `Vacant` guard. The inflight entry was already removed; the prune cycle only cleans up entries still present in `inflight_states`, so it provides no recovery for this block.

**The lookup API to perform the check already exists but is unused here:** [6](#0-5) 

**Contrast with the relay path**, which correctly gates processing on the sending peer being present in `peers_map` before touching shared state: [7](#0-6) 

## Impact Explanation

A connected peer can permanently stall a victim node at any specific block height by racing to send a `SendBlock` with a hash-matching but transaction-invalid block body. The node's sync loop stops advancing past that height because `BlockFetcher` will not re-request a block already marked `BLOCK_RECEIVED`/`BLOCK_INVALID`. The attack is repeatable across multiple block heights simultaneously (one attacker message per in-flight hash), and can be applied to many nodes in parallel with negligible cost, causing targeted but scalable CKB network congestion. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation

- Any peer that completes the CKB P2P handshake can send `SendBlock` messages; no key material or hashpower is required.
- In-flight block hashes are trivially observable: the attacker watches `GetBlocks` messages from the victim, or infers the sync frontier from public chain data.
- The race window is the victim's round-trip time to the legitimate peer — easily won by a co-located or low-latency attacker.
- The attack is repeatable: after the original attacker peer is banned, a new peer identity can repeat the attack on the next in-flight hash.

## Recommendation

Before calling `new_block_received` (or inside it, with a peer parameter), verify the sender:

1. Call `inflight_state_by_block(&(block.number(), block.hash()).into())` to retrieve the `InflightState`.
2. Compare `state.peer` with `self.peer` (the incoming `SendBlock` sender).
3. If they do not match, return early without removing the inflight entry or marking `BLOCK_RECEIVED`. Apply a mild penalty (not a ban) to the unsolicited sender, since unsolicited blocks can arrive legitimately during reorgs.

This mirrors the existing correct pattern in `BlockTransactionsProcess`.

## Proof of Concept

1. Connect malicious peer M to victim node V (standard P2P handshake).
2. Observe V sending `GetBlocks` containing hash `H` (block at the sync frontier).
3. Before the legitimate assigned peer responds, M sends a `SendBlock` message: header matches `H` exactly (valid hash), but the transaction list does not match `transactions_root`.
4. V's `BlockProcess::execute` calls `new_block_received`: `remove_by_block` succeeds (hash `H` is in-flight), status is `HEADER_VALID`, entry is `Vacant` → returns `true`. Inflight slot for `H` is consumed.
5. Asynchronous verification fails (`transactions_root` mismatch). M is banned. Block status transitions to `BLOCK_INVALID`.
6. On the next `BlockFetcher` cycle, `get_block_status(H)` returns a status containing `BLOCK_RECEIVED` (or `BLOCK_INVALID` which is a superset). The fetcher skips `H` at line 269. V's sync stalls at block `H`.
7. Repeat for additional in-flight hashes using fresh peer identities to stall V at multiple heights simultaneously.

### Citations

**File:** sync/src/synchronizer/block_process.rs (L43-43)
```rust
        if shared.new_block_received(&block) {
```

**File:** sync/src/types/mod.rs (L624-626)
```rust
    pub fn inflight_state_by_block(&self, block: &BlockNumberAndHash) -> Option<&InflightState> {
        self.inflight_states.get(block)
    }
```

**File:** sync/src/types/mod.rs (L791-796)
```rust
        self.inflight_states
            .remove(&block)
            .map(|state| {
                let elapsed = unix_time_as_millis().saturating_sub(state.timestamp);
                if let Some(set) = download_schedulers.get_mut(&state.peer) {
                    set.hashes.remove(&block);
```

**File:** sync/src/types/mod.rs (L1201-1207)
```rust
        if !self
            .state()
            .write_inflight_blocks()
            .remove_by_block((block.number(), block.hash()).into())
        {
            return false;
        }
```

**File:** sync/src/types/mod.rs (L1220-1225)
```rust
        if let dashmap::mapref::entry::Entry::Vacant(status) =
            self.shared().block_status_map().entry(block.hash())
        {
            status.insert(BlockStatus::BLOCK_RECEIVED);
            return true;
        }
```

**File:** sync/src/synchronizer/block_fetcher.rs (L269-271)
```rust
                } else if status.contains(BlockStatus::BLOCK_RECEIVED) {
                    // Do not download repeatedly
                } else if (matches!(self.ibd, IBDState::In)
```

**File:** sync/src/relayer/block_transactions_process.rs (L65-73)
```rust
        if let Entry::Occupied(mut pending) = shared
            .state()
            .pending_compact_blocks()
            .await
            .entry(block_hash.clone())
        {
            let (compact_block, peers_map, _) = pending.get_mut();
            if let Entry::Occupied(mut value) = peers_map.entry(self.peer) {
                let (expected_transaction_indexes, expected_uncle_indexes) = value.get_mut();
```
