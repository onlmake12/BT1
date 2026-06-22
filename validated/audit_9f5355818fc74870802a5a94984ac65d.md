The execution flow is now fully traced. The vulnerability is real and concrete. Here is the analysis:

---

### Title
Compact Block Relay Accepts Trivially-Mined Blocks with Wrong `compact_target`, Causing Unguarded State Mutations Before Contextual Rejection — (`sync/src/relayer/compact_block_process.rs`)

### Summary

The `HeaderVerifier` used during compact block relay (`verification/src/header_verifier.rs`) checks PoW validity against the block's **own** `compact_target` and checks epoch number continuity, but never checks that `compact_target` matches the epoch's expected difficulty. The `compact_target` mismatch check lives exclusively in `ContextualBlockVerifier` (`verification/contextual/src/contextual_block_verifier.rs`), which runs only during full block acceptance — after `insert_valid_header`, `pending_compact_blocks` insertion, and `GetBlockTransactions` have already been issued. CompactBlock messages also explicitly bypass the per-peer rate limiter.

### Finding Description

**Exact call sequence in `CompactBlockProcess::execute()`:**

1. `non_contextual_check()` — checks uncle/proposal counts and block height. No `compact_target` check. [1](#0-0) 

2. `contextual_check()` — calls `HeaderVerifier::verify()`: [2](#0-1) 

   Inside `HeaderVerifier::verify()`, the checks are:
   - `PowVerifier`: eaglesong output ≤ `compact_to_target(header.compact_target)` — passes if the block's PoW is valid for its **own** (attacker-chosen) target.
   - `NumberVerifier`: block number = parent + 1.
   - `EpochVerifier` (from `header_verifier.rs`): only checks `is_well_formed()` and `is_successor_of(parent_epoch)` — **no `compact_target` check**.
   - `TimestampVerifier`. [3](#0-2) 

   The `EpochVerifier` in `header_verifier.rs` explicitly does not check `compact_target`: [4](#0-3) 

3. After `contextual_check()` passes, `insert_valid_header()` is called — **HeaderMap mutation**: [5](#0-4) [6](#0-5) 

4. If block reconstruction is missing transactions, `missing_or_collided_post_process()` is called — **`pending_compact_blocks` insertion + `GetBlockTransactions` sent**: [7](#0-6) [8](#0-7) 

5. Only when the full block is later accepted does `ContextualBlockVerifier` run `EpochVerifier::verify()`, which finally checks `compact_target`: [9](#0-8) 

   At this point the block is marked `BLOCK_INVALID`, but all state mutations from steps 3–4 have already occurred.

**Rate limiting is explicitly disabled for CompactBlock:** [10](#0-9) 

### Impact Explanation

An unprivileged attacker can:

- Set `compact_target = 0x207fffff` (minimum difficulty) in a crafted block header. `compact_to_target(0x207fffff)` yields a target near `2^256 - 1`, so **any nonce produces a valid eaglesong output** — zero real PoW cost.
- Set the correct epoch number (valid successor of the parent's epoch) so the `EpochVerifier` in `header_verifier.rs` passes.
- Send the block as a `CompactBlock` with at least one short-ID transaction not in the local tx-pool, triggering the `ReconstructionResult::Missing` path.

Per crafted block, the target node:
1. Inserts a `HeaderIndexView` into the `HeaderMap` (persistent in-memory/sled store).
2. Inserts an entry into `pending_compact_blocks`.
3. Sends a `GetBlockTransactions` message to the attacker peer — a wasted network round-trip.

Since there is no rate limit on `CompactBlock` messages, the attacker can repeat this at the maximum network throughput, causing unbounded `HeaderMap` growth, `pending_compact_blocks` accumulation, and amplified outbound `GetBlockTransactions` traffic.

### Likelihood Explanation

The attack requires only a P2P connection to the target node and knowledge of the current chain tip (publicly available). No hashpower, no privileged access, no key material. The PoW cost is effectively zero. The attack is locally testable and repeatable.

### Recommendation

Move the `compact_target` epoch consistency check into the relay-time `HeaderVerifier` (or add it as an explicit check in `contextual_check()` before `insert_valid_header` is called). Specifically, after verifying the parent exists, compute the expected `compact_target` for the block's epoch from the parent's `EpochExt` and reject the compact block if it does not match — mirroring the check already present in `ContextualBlockVerifier::EpochVerifier::verify()`.

Additionally, consider applying rate limiting to `CompactBlock` messages with a per-peer token bucket, or at minimum banning peers that repeatedly send compact blocks with invalid headers.

### Proof of Concept

```
1. Connect to a target CKB node via P2P (RelayV3 protocol).
2. Observe the current tip header to obtain: parent_hash, parent_epoch, block_number.
3. Construct a RawHeader with:
     - parent_hash = tip.hash
     - number      = tip.number + 1
     - epoch       = valid successor of tip.epoch (correct epoch number, any fraction)
     - compact_target = 0x207fffff   ← attacker-chosen, minimum difficulty
     - timestamp   = tip.timestamp + 1 (within allowed window)
4. Compute calc_pow_hash over the RawHeader (includes compact_target).
5. Build pow_message = pow_hash(32 bytes) || nonce_le(16 bytes) with nonce = 0.
   eaglesong(pow_message) will be ≤ compact_to_target(0x207fffff) ≈ 2^256-1 for any nonce.
6. Wrap in a CompactBlock with one short_id referencing a tx not in the target's tx-pool
   (ensures ReconstructionResult::Missing path).
7. Send the CompactBlock message.

Expected result:
  - Target node calls insert_valid_header (HeaderMap insertion).
  - Target node inserts into pending_compact_blocks.
  - Target node sends GetBlockTransactions back to attacker.
  - Repeat from step 3 with a new nonce/timestamp; no rate limit applies.
  - Each iteration costs the attacker ~0 CPU; each costs the target one HeaderMap entry,
    one pending_compact_blocks entry, and one outbound GetBlockTransactions message.
```

### Citations

**File:** sync/src/relayer/compact_block_process.rs (L64-68)
```rust
        let status =
            non_contextual_check(&compact_block, &header, shared.consensus(), &active_chain);
        if !status.is_ok() {
            return status;
        }
```

**File:** sync/src/relayer/compact_block_process.rs (L70-73)
```rust
        let status = contextual_check(&header, shared, &active_chain, &self.nc, self.peer).await;
        if !status.is_ok() {
            return status;
        }
```

**File:** sync/src/relayer/compact_block_process.rs (L77-78)
```rust
        // Header has been verified ok, update state
        shared.insert_valid_header(self.peer, &header);
```

**File:** sync/src/relayer/compact_block_process.rs (L128-151)
```rust
            ReconstructionResult::Missing(transactions, uncles) => {
                let missing_transactions: Vec<u32> =
                    transactions.into_iter().map(|i| i as u32).collect();

                if let Some(metrics) = ckb_metrics::handle() {
                    metrics
                        .ckb_relay_cb_fresh_tx_cnt
                        .inc_by(missing_transactions.len() as u64);
                    metrics.ckb_relay_cb_reconstruct_fail.inc();
                }

                let missing_uncles: Vec<u32> = uncles.into_iter().map(|i| i as u32).collect();
                missing_or_collided_post_process(
                    compact_block,
                    block_hash.clone(),
                    shared,
                    self.nc,
                    missing_transactions,
                    missing_uncles,
                    self.peer,
                )
                .await;

                StatusCode::CompactBlockRequiresFreshTransactions.with_context(&block_hash)
```

**File:** sync/src/relayer/compact_block_process.rs (L354-378)
```rust
    shared
        .state()
        .pending_compact_blocks()
        .await
        .entry(block_hash.clone())
        .or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))
        .1
        .insert(peer, (missing_transactions.clone(), missing_uncles.clone()));

    let content = packed::GetBlockTransactions::new_builder()
        .block_hash(block_hash)
        .indexes(missing_transactions.as_slice())
        .uncle_indexes(missing_uncles.as_slice())
        .build();
    let message = packed::RelayMessage::new_builder().set(content).build();
    shared.shared().async_handle().spawn(async move {
        let sending = async_send_message_to(&nc, peer, &message).await;
        if !sending.is_ok() {
            ckb_logger::warn_target!(
                crate::LOG_TARGET_RELAY,
                "ignore the sending message error, error: {}",
                sending
            );
        }
    });
```

**File:** verification/src/header_verifier.rs (L32-50)
```rust
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
        let parent_fields = self
            .data_loader
            .get_header_fields(&header.parent_hash())
            .ok_or_else(|| UnknownParentError {
                parent_hash: header.parent_hash(),
            })?;
        NumberVerifier::new(parent_fields.number, header).verify()?;
        EpochVerifier::new(parent_fields.epoch, header).verify()?;
        TimestampVerifier::new(
            self.data_loader,
            header,
            self.consensus.median_time_block_count(),
        )
        .verify()?;
        Ok(())
    }
```

**File:** verification/src/header_verifier.rs (L133-148)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        if !self.header.epoch().is_well_formed() {
            return Err(EpochError::Malformed {
                value: self.header.epoch(),
            }
            .into());
        }
        if !self.parent.is_genesis() && !self.header.epoch().is_successor_of(self.parent) {
            return Err(EpochError::NonContinuous {
                current: self.header.epoch(),
                parent: self.parent,
            }
            .into());
        }
        Ok(())
    }
```

**File:** sync/src/types/mod.rs (L1129-1132)
```rust
        self.shared.header_map().insert(header_view.clone());
        self.state
            .peers()
            .may_set_best_known_header(peer, header_view.as_header_index());
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L500-507)
```rust
        let actual_compact_target = header.compact_target();
        if self.epoch.compact_target() != actual_compact_target {
            return Err(EpochError::TargetMismatch {
                expected: self.epoch.compact_target(),
                actual: actual_compact_target,
            }
            .into());
        }
```

**File:** sync/src/relayer/mod.rs (L112-114)
```rust
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));
```
