Now I have all the evidence needed. Let me trace the complete path precisely.

### Title
PoW Verifier Accepts Attacker-Supplied `compact_target`, Bypassing Rate-Limit Assumption for CompactBlock Relay — (`pow/src/eaglesong_blake2b.rs`)

---

### Summary

`EaglesongBlake2bPowEngine::verify` validates the nonce against the `compact_target` embedded in the attacker-supplied header, not the epoch-mandated target. Setting `compact_target=0x20ffffff` (which decodes to `U256::max_value()`) causes any nonce to pass PoW. The relay path's `HeaderVerifier` does not cross-check `compact_target` against the epoch, and CompactBlock messages are **explicitly exempt from rate limiting** on the assumption that PoW is the natural rate limiter. Each fake block passes `HeaderVerifier`, triggers a DB write, and is only rejected later by the contextual `EpochVerifier` — after resources have been consumed.

---

### Finding Description

**Step 1 — `compact_target=0x20ffffff` decodes to `U256::max_value()`**

The codebase's own test confirms this: [1](#0-0) 

`compact_to_target(0x20ffffff)`: exponent=32, mantissa=0xffffff. The overflow guard is `!mantissa.is_zero() && (exponent > 32)` — since 32 is not > 32, overflow = false. [2](#0-1) 

Result: target = `U256::max_value()`, not zero, no overflow.

**Step 2 — `EaglesongBlake2bPowEngine::verify` uses the header's own `compact_target`** [3](#0-2) 

The engine reads `header.raw().compact_target()` directly. With target = `U256::max_value()`, the comparison `hash_output > block_target` is always false → `verify` returns `true` for **any nonce**.

**Step 3 — `HeaderVerifier::verify` in the relay path does not check `compact_target` against the epoch** [4](#0-3) 

- `PowVerifier` calls `EaglesongBlake2bPowEngine::verify` → passes (step 2).
- `EpochVerifier` (the one in `header_verifier.rs`) only checks epoch continuity (`is_well_formed`, `is_successor_of`), **not** `compact_target`: [5](#0-4) 

The `compact_target` vs. epoch check only exists in `ContextualBlockVerifier::EpochVerifier`: [6](#0-5) 

This is called **much later** in the pipeline.

**Step 4 — CompactBlock messages are exempt from rate limiting** [7](#0-6) 

The comment explicitly states the design assumption: PoW is the rate limiter for CompactBlock. With PoW bypassed, this protection is gone.

**Step 5 — DB write occurs before the contextual epoch check**

The relay path calls `accept_block` → `accept_remote_block` → `ChainService::asynchronous_process_block`: [8](#0-7) 

`insert_block` writes the block to the DB **before** the orphan broker runs contextual verification. The contextual `EpochVerifier` only fires after this write.

**Step 6 — Peer banning is asynchronous** [9](#0-8) 

The ban callback fires only after the full async verification pipeline completes. An attacker can pipeline many CompactBlock messages before the first ban takes effect.

---

### Impact Explanation

Each crafted block:
1. Passes `HeaderVerifier` (PoW + epoch continuity) — zero real computation required.
2. Triggers `insert_valid_header`, block reconstruction, and a DB write.
3. Is only rejected by `ContextualBlockVerifier::EpochVerifier` after the DB write.

Because CompactBlock messages bypass the rate limiter, an attacker with even a small number of peer connections can flood nodes with zero-cost "valid PoW" blocks, consuming DB I/O, parent-lookup, and epoch-calculation resources on every connected node before each block is rejected.

---

### Likelihood Explanation

- Requires only an unprivileged P2P connection (standard relay path).
- No real hash power needed — any nonce works.
- The attacker must supply a valid parent hash, block number, epoch field, and timestamp, all of which are trivially derivable from the public chain tip.
- Peer banning limits sustained single-peer attacks, but the attacker can rotate peers or pipeline many messages before the async ban fires.
- Applies specifically to the EaglesongBlake2b engine (testnet), not mainnet Eaglesong.

---

### Recommendation

`EaglesongBlake2bPowEngine::verify` (and `EaglesongPowEngine::verify`) should not be the sole PoW gate. The relay path's `HeaderVerifier` should validate that `header.compact_target()` matches the epoch-derived target **before** accepting the header as PoW-valid, or the rate limiter exemption for CompactBlock should be removed/bounded independently of PoW correctness.

---

### Proof of Concept

```rust
// Unit test: any nonce passes with compact_target=0x20ffffff
let header = HeaderBuilder::default()
    .compact_target(0x20ffffff_u32)
    .nonce(0u128)  // arbitrary
    .build();
let engine = EaglesongBlake2bPowEngine;
assert!(engine.verify(&header.data())); // passes for any nonce
```

For a live node: connect as a peer, observe the chain tip (get parent hash, number, epoch, timestamp), craft a `CompactBlock` with `compact_target=0x20ffffff` and any nonce, send repeatedly. Each message bypasses the rate limiter, passes `HeaderVerifier`, triggers a DB write, and is rejected only after the write completes.

### Citations

**File:** util/types/src/utilities/tests/difficulty.rs (L25-35)
```rust
        let compact_when_target_is_max = 0x20ffffff;

        let compact = target_to_compact(U256::max_value());
        assert_eq!(compact, compact_when_target_is_max);

        let difficulty = compact_to_difficulty(compact);
        assert_eq!(difficulty, U256::one());

        let compact_from_difficulty = difficulty_to_compact(difficulty);
        assert_eq!(compact, compact_from_difficulty);
    }
```

**File:** util/types/src/utilities/difficulty.rs (L62-77)
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
