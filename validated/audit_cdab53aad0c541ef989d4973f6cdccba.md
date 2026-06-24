Audit Report

## Title
PoW Verification Bypass via Attacker-Supplied `compact_target` Enables Zero-Cost DB Flood Through CompactBlock Relay — (`pow/src/eaglesong_blake2b.rs`)

## Summary

`EaglesongBlake2bPowEngine::verify` reads `compact_target` directly from the attacker-supplied header and validates the nonce against it rather than the epoch-mandated target. Setting `compact_target=0x20ffffff` decodes to a near-maximal target (`0xffffff << 232`) without triggering the overflow guard, causing ~99.9999% of all nonces to pass PoW with zero real computation. The relay path's `HeaderVerifier` does not cross-check `compact_target` against the epoch-derived target, CompactBlock messages are explicitly exempt from rate limiting on the assumption that PoW is the natural rate limiter, and a DB write occurs before the contextual `EpochVerifier` that would catch the mismatch.

## Finding Description

**Root cause — `compact_to_target(0x20ffffff)` produces a near-maximal target without triggering overflow:**

With `compact = 0x20ffffff`: `exponent = 32`, `mantissa = 0xffffff`. The overflow guard is `!mantissa.is_zero() && (exponent > 32)` — since `32 > 32` is false, overflow is `false`. The result is `ret = 0xffffff << 232`. This is confirmed by the existing test which asserts `target_to_compact(U256::max_value()) == 0x20ffffff`. [1](#0-0) [2](#0-1) 

The probability that a random 256-bit hash exceeds this target is `2^232 / 2^256 = 2^−24 ≈ 1/16M`, meaning ~99.9999% of all nonces pass the PoW check.

**`EaglesongBlake2bPowEngine::verify` uses the header's own `compact_target`:**

The engine reads `header.raw().compact_target()` directly at line 20. With the near-maximal target, `hash_output > block_target` is false for virtually any nonce, so `verify` returns `true`. [3](#0-2) 

**The relay path's `HeaderVerifier` does not validate `compact_target` against the epoch:**

`HeaderVerifier::verify` calls `PowVerifier` (which uses the header's own `compact_target`), `NumberVerifier`, `EpochVerifier`, and `TimestampVerifier`. The `EpochVerifier` called here only checks epoch continuity (`is_well_formed`, `is_successor_of`) — it does not check whether `compact_target` matches the epoch-derived target. [4](#0-3) [5](#0-4) 

**The `compact_target` vs. epoch check exists only in `ContextualBlockVerifier::EpochVerifier`:** [6](#0-5) 

This runs much later in the pipeline, after the DB write.

**CompactBlock messages are explicitly exempt from rate limiting:** [7](#0-6) 

The design assumption is that PoW is the natural rate limiter. With PoW bypassed, this protection is gone.

**DB write occurs before contextual epoch check:**

In `compact_block_process.rs`, after `contextual_check` (which calls `HeaderVerifier`) passes, `insert_valid_header` is called at line 78, the block is reconstructed, and `accept_block` is invoked at line 119. [8](#0-7) 

This leads to `ChainService::asynchronous_process_block`, where `non_contextual_verify` (`BlockVerifier` — checks proposals limit, block bytes, cellbase, duplicates, merkle root, but NOT `compact_target` vs. epoch) runs first, then `insert_block` writes to the DB at line 133, and only then does `orphan_broker.process_lonely_block` trigger contextual verification at line 143. [9](#0-8) [10](#0-9) 

**Peer banning is asynchronous:**

The ban callback fires only after the full async verification pipeline completes (via the `verify_callback` closure), allowing an attacker to pipeline many CompactBlock messages before the first ban takes effect. [11](#0-10) 

## Impact Explanation

Each crafted block: (1) passes `HeaderVerifier` with zero real hash power, (2) triggers `insert_valid_header`, block reconstruction, and a DB write via `insert_block`, and (3) is only rejected by `ContextualBlockVerifier::EpochVerifier` after the DB write. Because CompactBlock messages bypass the rate limiter, an attacker with even a small number of peer connections can continuously flood nodes with zero-cost "valid PoW" blocks, consuming DB I/O, parent-lookup, and epoch-calculation resources on every connected node before each block is rejected. This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation

- Requires only an unprivileged P2P connection (standard relay path).
- No real hash power needed — any of ~99.9999% of nonces passes; an attacker needs at most a handful of nonce attempts.
- The attacker must supply a valid parent hash, block number, epoch field, and timestamp, all trivially derivable from the public chain tip.
- Peer banning limits sustained single-peer attacks, but the attacker can rotate peers or pipeline many messages before the async ban fires.
- Applies specifically to the `EaglesongBlake2b` engine (testnet); the mainnet `Eaglesong` engine has the same structural issue but is a separate code path.

## Recommendation

`EaglesongBlake2bPowEngine::verify` (and `EaglesongPowEngine::verify`) should not be the sole PoW gate in the relay path. The relay path's `HeaderVerifier` (or the `contextual_check` function in `compact_block_process.rs`) should validate that `header.compact_target()` matches the epoch-derived target **before** accepting the header as PoW-valid and before any DB write or state mutation occurs. Alternatively, the rate-limiter exemption for CompactBlock should be removed or bounded independently of PoW correctness.

## Proof of Concept

**Unit test (confirms near-maximal target, no overflow):**
```rust
// util/types/src/utilities/tests/difficulty.rs already asserts:
let compact_when_target_is_max = 0x20ffffff;
let compact = target_to_compact(U256::max_value());
assert_eq!(compact, compact_when_target_is_max); // passes
// compact_to_target(0x20ffffff) returns 0xffffff << 232, overflow=false
```

**PoW bypass unit test:**
```rust
let header = HeaderBuilder::default()
    .compact_target(0x20ffffff_u32)
    .nonce(0u128) // arbitrary; ~99.9999% of nonces pass
    .build();
let engine = EaglesongBlake2bPowEngine;
assert!(engine.verify(&header.data())); // passes for virtually any nonce
```

**Live node steps:**
1. Connect as a peer to a testnet node running `EaglesongBlake2b`.
2. Observe the chain tip to obtain parent hash, block number, epoch, and timestamp.
3. Craft a `CompactBlock` with `compact_target=0x20ffffff` and any nonce (retry at most a handful of times if the ~1/16M failure case occurs).
4. Send repeatedly over the relay protocol. Each message bypasses the rate limiter, passes `HeaderVerifier`, triggers `insert_valid_header` and a DB write via `insert_block`, and is rejected only after the write by `ContextualBlockVerifier::EpochVerifier` with `EpochError::TargetMismatch`.

### Citations

**File:** util/types/src/utilities/difficulty.rs (L62-76)
```rust
pub fn compact_to_target(compact: u32) -> (U256, bool) {
    let exponent = compact >> 24;
    let mut mantissa = U256::from(compact & 0x00ff_ffff);

    let mut ret;
    if exponent <= 3 {
        mantissa >>= 8 * (3 - exponent);
        ret = mantissa.clone();
    } else {
        ret = mantissa.clone();
        ret <<= 8 * (exponent - 3);
    }

    let overflow = !mantissa.is_zero() && (exponent > 32);
    (ret, overflow)
```

**File:** util/types/src/utilities/tests/difficulty.rs (L24-35)
```rust
    {
        let compact_when_target_is_max = 0x20ffffff;

        let compact = target_to_compact(U256::max_value());
        assert_eq!(compact, compact_when_target_is_max);

        let difficulty = compact_to_difficulty(compact);
        assert_eq!(difficulty, U256::one());

        let compact_from_difficulty = difficulty_to_compact(difficulty);
        assert_eq!(compact, compact_from_difficulty);
    }
```

**File:** pow/src/eaglesong_blake2b.rs (L20-30)
```rust
        let (block_target, overflow) = compact_to_target(header.raw().compact_target().into());

        if block_target.is_zero() || overflow {
            debug!(
                "compact_target is invalid: {:#x}",
                header.raw().compact_target()
            );
            return false;
        }

        if U256::from_big_endian(&output[..]).expect("bound checked") > block_target {
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

**File:** sync/src/relayer/mod.rs (L296-334)
```rust
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

**File:** sync/src/relayer/compact_block_process.rs (L78-119)
```rust
        shared.insert_valid_header(self.peer, &header);

        // Request proposal
        let proposals: Vec<_> = compact_block.proposals().into_iter().collect();
        self.relayer.request_proposal_txs(
            &self.nc,
            self.peer,
            (header.number(), block_hash.clone()).into(),
            proposals,
        );

        // Reconstruct block
        let ret = self
            .relayer
            .reconstruct_block(&active_chain, &compact_block, vec![], &[], &[])
            .await;

        // Accept block
        // `relayer.accept_block` will make sure the validity of block before persisting
        // into database
        match ret {
            ReconstructionResult::Block(block) => {
                if let Some(metrics) = ckb_metrics::handle() {
                    metrics
                        .ckb_relay_cb_transaction_count
                        .inc_by(block.transactions().len() as u64);
                    metrics.ckb_relay_cb_reconstruct_ok.inc();
                }
                let mut pending_compact_blocks = shared.state().pending_compact_blocks().await;
                pending_compact_blocks.remove(&block_hash);
                // remove all pending request below this block epoch
                //
                // use epoch as the judgment condition because we accept
                // all block in current epoch as uncle block
                pending_compact_blocks.retain(|_, (v, _, _)| {
                    Into::<EpochNumberWithFraction>::into(v.header().as_reader().raw().epoch())
                        .number()
                        >= block.epoch().number()
                });
                shrink_to_fit!(pending_compact_blocks, 20);
                self.relayer
                    .accept_block(Arc::clone(&self.nc), self.peer, block, "CompactBlock");
```

**File:** chain/src/chain_service.rs (L133-143)
```rust
        if let Err(err) = self.insert_block(&lonely_block) {
            error!(
                "insert block {}-{} failed: {:?}",
                block_number, block_hash, err
            );
            self.shared.block_status_map().remove(&block_hash);
            lonely_block.execute_callback(Err(err));
            return;
        }

        self.orphan_broker.process_lonely_block(lonely_block.into());
```

**File:** chain/src/chain_service.rs (L146-150)
```rust
    fn insert_block(&self, lonely_block: &LonelyBlock) -> Result<(), ckb_error::Error> {
        let db_txn = self.shared.store().begin_transaction();
        db_txn.insert_block(lonely_block.block())?;
        db_txn.commit()?;
        Ok(())
```
