All cited code references check out against the actual repository. Let me verify one more detail about pending_compact_blocks cleanup.

Audit Report

## Title
Compact Block Relay Accepts Zero-Cost PoW Blocks with Attacker-Chosen `compact_target`, Causing Unguarded State Mutations Before Contextual Rejection — (`sync/src/relayer/compact_block_process.rs`)

## Summary

The `HeaderVerifier` used during compact block relay checks PoW validity only against the block's **own** `compact_target`, and its `EpochVerifier` checks only epoch number continuity — never that `compact_target` matches the epoch's expected difficulty. The `compact_target` consistency check exists exclusively in `ContextualBlockVerifier`, which runs only during full block acceptance. This means `insert_valid_header` (HeaderMap mutation) and `pending_compact_blocks` insertion with outbound `GetBlockTransactions` are issued for blocks whose `compact_target` is entirely attacker-controlled. CompactBlock messages explicitly bypass the per-peer rate limiter, making the attack unbounded.

## Finding Description

**Execution flow in `CompactBlockProcess::execute()`:**

1. `non_contextual_check()` — checks uncle/proposal counts and block height. No `compact_target` check. [1](#0-0) 

2. `contextual_check()` — calls `HeaderVerifier::verify()`: [2](#0-1) 

   Inside `HeaderVerifier::verify()`, the sub-checks are: `PowVerifier` (eaglesong output ≤ `compact_to_target(header.compact_target)` — passes trivially when the attacker sets their own easy target), `NumberVerifier`, `EpochVerifier`, and `TimestampVerifier`. [3](#0-2) 

   The `EpochVerifier` in `header_verifier.rs` only checks `is_well_formed()` and `is_successor_of(parent_epoch)`. There is **no `compact_target` check**: [4](#0-3) 

3. After `contextual_check()` passes, `insert_valid_header()` is called — **HeaderMap mutation** (in-memory LRU + sled backend): [5](#0-4) [6](#0-5) 

4. If block reconstruction has missing transactions, `missing_or_collided_post_process()` is called — **`pending_compact_blocks` insertion + `GetBlockTransactions` sent to attacker**: [7](#0-6) [8](#0-7) 

5. Only when the full block is later accepted does `ContextualBlockVerifier::EpochVerifier::verify()` run the `compact_target` consistency check: [9](#0-8) 

   At this point the block is marked `BLOCK_INVALID`, but all state mutations from steps 3–4 have already occurred and are not rolled back.

**`pending_compact_blocks` has no timeout-based eviction.** The only cleanup calls in `compact_block_process.rs` are triggered by successful block reconstruction/acceptance. Entries for blocks that are never completed (attacker never sends the requested transactions) persist indefinitely.

**Rate limiting is explicitly disabled for `CompactBlock`:** [10](#0-9) 

**PoW bypass confirmed:** `EaglesongPowEngine::verify()` computes `compact_to_target(header.compact_target)` and checks only that the result is non-zero and non-overflow. With `compact_target = 0x207fffff` (exponent 32, mantissa 0x7fffff), the target is `0x7fffff << 232` — a 255-bit value that fits in U256, is non-zero, and does not overflow. Any eaglesong output will be ≤ this target, so **any nonce passes PoW verification at zero real cost**. [11](#0-10) 

## Impact Explanation

An attacker can drive unbounded growth of the `HeaderMap` (memory + sled disk store) and `pending_compact_blocks` map, and generate amplified outbound `GetBlockTransactions` traffic — all at effectively zero PoW cost and with no rate limit. Sustained attack causes memory exhaustion and node crash (**High: Vulnerabilities which could easily crash a CKB node**) and/or saturates outbound bandwidth (**High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**).

## Likelihood Explanation

The attack requires only a P2P connection to the target node and knowledge of the current chain tip (publicly available via RPC or peer gossip). No hashpower, no privileged access, no key material. The PoW cost is effectively zero. The attack is locally testable, repeatable at maximum network throughput, and can be executed by any unprivileged external user.

## Recommendation

Move the `compact_target` epoch consistency check into the relay-time `HeaderVerifier` (or add it as an explicit check in `contextual_check()` before `insert_valid_header` is called). After verifying the parent exists, compute the expected `compact_target` for the block's epoch from the parent's `EpochExt` and reject the compact block if it does not match — mirroring the check already present in `ContextualBlockVerifier::EpochVerifier::verify()`. Additionally, apply a per-peer token bucket rate limit to `CompactBlock` messages, or at minimum ban peers that repeatedly send compact blocks with invalid headers.

## Proof of Concept

```
1. Connect to a target CKB node via P2P (RelayV3 protocol).
2. Observe the current tip header to obtain: parent_hash, parent_epoch, block_number.
3. Construct a RawHeader with:
     - parent_hash    = tip.hash
     - number         = tip.number + 1
     - epoch          = valid successor of tip.epoch (correct epoch number, any fraction)
     - compact_target = 0x207fffff   ← attacker-chosen, minimum difficulty
     - timestamp      = tip.timestamp + 1 (within allowed window)
4. Compute calc_pow_hash over the RawHeader (includes compact_target).
5. Build pow_message = pow_hash(32 bytes) || nonce_le(16 bytes) with nonce = 0.
   eaglesong(pow_message) will be ≤ compact_to_target(0x207fffff) ≈ 2^255 for any nonce.
6. Wrap in a CompactBlock with one short_id referencing a tx not in the target's tx-pool
   (ensures ReconstructionResult::Missing path).
7. Send the CompactBlock message. Never respond to the GetBlockTransactions reply.

Expected result per iteration:
  - Target node calls insert_valid_header → HeaderMap insertion (memory + sled).
  - Target node inserts into pending_compact_blocks (never evicted).
  - Target node sends GetBlockTransactions back to attacker (wasted outbound bandwidth).
  - Repeat from step 3 with a new nonce/timestamp; no rate limit applies.
  - Each iteration costs the attacker ~0 CPU; each costs the target one HeaderMap entry,
    one pending_compact_blocks entry, and one outbound GetBlockTransactions message.
  - Sustained attack exhausts memory and crashes the node.
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

**File:** pow/src/eaglesong.rs (L11-27)
```rust
    fn verify(&self, header: &Header) -> bool {
        let input = crate::pow_message(&header.as_reader().calc_pow_hash(), header.nonce().into());
        let mut output = [0u8; 32];
        eaglesong(&input, &mut output);

        let (block_target, overflow) = compact_to_target(header.raw().compact_target().into());

        if block_target.is_zero() || overflow {
            debug!(
                "compact_target is invalid: {:#x}",
                header.raw().compact_target()
            );
            return false;
        }

        if U256::from_big_endian(&output[..]).expect("bound checked") > block_target {
            if log_enabled!(Debug) {
```
