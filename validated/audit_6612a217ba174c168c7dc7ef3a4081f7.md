All the necessary code has been traced. Here is the complete analysis:

---

### Title
Missing `compact_target` Epoch Validation in `HeaderVerifier` Allows Zero-Work Header Flooding — (`verification/src/header_verifier.rs`)

### Summary

`HeaderVerifier::verify` in the sync path validates PoW only against the header's *own* `compact_target`, but never checks that `compact_target` matches the epoch's consensus-enforced target. An unprivileged peer can craft headers with `compact_target=0x20ffffff` (which `compact_to_target` decodes to `0xffffff << 232` with `overflow=false`), making the PoW trivially satisfiable by essentially any nonce. These headers pass all checks in `HeaderAcceptor::accept` and are inserted into the node's `header_map` via `insert_valid_header`.

### Finding Description

**`compact_to_target(0x20ffffff)` behavior** — confirmed by the test suite: [1](#0-0) 

`target_to_compact(U256::max_value()) == 0x20ffffff`. The inverse `compact_to_target(0x20ffffff)` returns `(0xffffff << 232, false)` — a 256-bit value with the top 24 bits all set and the remaining 232 bits zero. The overflow guard is `exponent > 32`; since `0x20 = 32`, `overflow = false`. [2](#0-1) 

**`EaglesongBlake2bPowEngine::verify` accepts this target** — the guard `block_target.is_zero() || overflow` is false for this value, so the engine proceeds to check `output <= 0xffffff00...00`. Because the top 24 bits of the target are all `0xff`, approximately `(2^24 - 1)/2^24 ≈ 99.9999%` of all hash outputs satisfy this condition. Nonce=0 works with near-certainty. [3](#0-2) 

**`HeaderVerifier::verify` never checks `compact_target` against the epoch** — it calls the local `EpochVerifier` which only validates epoch number continuity (`is_well_formed` and `is_successor_of`), not the `compact_target` value: [4](#0-3) [5](#0-4) 

The `compact_target` vs. epoch check exists only in the contextual block verifier's `EpochVerifier`, which is never reached during header-only sync: [6](#0-5) 

**The sync path inserts the fake header unconditionally** — `HeaderAcceptor::accept` calls `non_contextual_check` (which calls `HeaderVerifier::verify`), then on success calls `sync_shared.insert_valid_header`: [7](#0-6) 

**`insert_valid_header` stores the header in `header_map` and updates peer/shared best state**, computing `total_difficulty = parent_total_difficulty + header.difficulty()`. With `compact_target=0x20ffffff`, `difficulty = 1`, so the fake chain's total difficulty grows by 1 per header — it cannot easily displace the real chain tip, but the headers are stored regardless: [8](#0-7) 

### Impact Explanation

An attacker can send `SendHeaders` P2P messages (up to `MAX_HEADERS_LEN = 2000` headers per message) containing headers with `compact_target=0x20ffffff` and any nonce. Each header requires zero real PoW. All such headers pass `HeaderVerifier::verify` and are inserted into the node's `header_map`. Effects:

1. **Memory/disk pressure**: The `header_map` has a configurable memory limit; fake headers can evict legitimate headers, degrading sync performance.
2. **Bandwidth waste**: The node may issue `GetBlocks` requests for fake headers it believes are valid.
3. **CPU overhead**: Processing 2000 fake headers per message at negligible cost to the attacker.
4. **Peer best-header poisoning**: `may_set_best_known_header` is updated with the fake header index, potentially distorting the node's view of peer chain tips.

The fake headers cannot enter the canonical chain (contextual block verification would reject them via `EpochVerifier::TargetMismatch`), but the header-chain pollution and resource exhaustion are real.

### Likelihood Explanation

Any unprivileged peer connected via the Sync protocol can exploit this. The `SendHeaders` message is a standard sync protocol message processed by `HeadersProcess::execute`. No special privileges, keys, or majority hashpower are required. The cost per fake header is essentially zero.

### Recommendation

Add a `compact_target` check to the `EpochVerifier` in `verification/src/header_verifier.rs`. The verifier already receives `parent_fields.epoch` (an `EpochNumberWithFraction`); to check `compact_target`, the `HeaderVerifier` needs access to the parent's `EpochExt` (not just `EpochNumberWithFraction`). The `SyncShared` already has `get_epoch_ext` available. Alternatively, add a standalone `CompactTargetVerifier` that retrieves the epoch's expected `compact_target` from the store and compares it against `header.compact_target()`, mirroring the check already present in `contextual_block_verifier.rs`.

### Proof of Concept

1. Connect to a CKB node as a peer via the Sync protocol.
2. Obtain the current tip header (parent).
3. Construct a `HeaderView` with:
   - `parent_hash` = tip hash
   - `number` = tip number + 1
   - `epoch` = valid successor of tip epoch (e.g., same epoch, index + 1)
   - `compact_target` = `0x20ffffff`
   - `timestamp` = tip timestamp + 1 (within allowed window)
   - `nonce` = 0 (passes PoW with ~99.9999% probability)
4. Send a `SendHeaders` P2P message containing this header.
5. Assert: `HeaderAcceptor::accept` returns `ValidationState::Valid` and the header appears in `header_map`.
6. Repeat with chained headers (each referencing the previous fake header as parent) to build an arbitrarily long zero-work header chain stored in the node.

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

**File:** verification/src/header_verifier.rs (L123-148)
```rust
pub struct EpochVerifier<'a> {
    parent: EpochNumberWithFraction,
    header: &'a HeaderView,
}

impl<'a> EpochVerifier<'a> {
    pub fn new(parent: EpochNumberWithFraction, header: &'a HeaderView) -> Self {
        EpochVerifier { parent, header }
    }

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

**File:** sync/src/synchronizer/headers_process.rs (L334-357)
```rust
        if let Some(is_invalid) = self.non_contextual_check(&mut result).err() {
            debug!(
                "HeadersProcess rejected non-contextual header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            if is_invalid {
                shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            }
            return result;
        }

        if self.version_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-version header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }

        sync_shared.insert_valid_header(self.peer, self.header);
        result
```

**File:** sync/src/types/mod.rs (L1094-1141)
```rust
    pub fn insert_valid_header(&self, peer: PeerIndex, header: &core::HeaderView) {
        let tip_number = self.active_chain().tip_number();
        let store_first = tip_number >= header.number();
        // We don't use header#parent_hash clone here because it will hold the arc counter of the SendHeaders message
        // which will cause the 2000 headers to be held in memory for a long time
        let parent_hash = Byte32::from_slice(header.data().raw().parent_hash().as_slice())
            .expect("checked slice length");
        let parent_header_index = self
            .get_header_index_view(&parent_hash, store_first)
            .expect("parent should be verified");
        let mut header_view = HeaderIndexView::new(
            header.hash(),
            header.number(),
            header.epoch(),
            header.timestamp(),
            parent_hash,
            parent_header_index.total_difficulty() + header.difficulty(),
        );

        let snapshot = Arc::clone(&self.shared.snapshot());
        header_view.build_skip(
            tip_number,
            |hash, store_first| self.get_header_index_view(hash, store_first),
            |number, current| {
                // shortcut to return an ancestor block
                if current.number <= snapshot.tip_number() && snapshot.is_main_chain(&current.hash)
                {
                    snapshot
                        .get_block_hash(number)
                        .and_then(|hash| self.get_header_index_view(&hash, true))
                } else {
                    None
                }
            },
        );
        self.shared.header_map().insert(header_view.clone());
        self.state
            .peers()
            .may_set_best_known_header(peer, header_view.as_header_index());
        if header_view.number().is_multiple_of(10000) {
            info!(
                "inserted valid header: header {}-{}",
                header_view.number(),
                header_view.hash()
            );
        }
        self.state.may_set_shared_best_header(header_view);
    }
```
