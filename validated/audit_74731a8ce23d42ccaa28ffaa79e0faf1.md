Audit Report

## Title
Missing `compact_target` Epoch Validation in Compact Block Relay Header Verifier Enables Zero-Work DoS — (`sync/src/relayer/compact_block_process.rs`, `verification/src/header_verifier.rs`)

## Summary
The `HeaderVerifier` used in the compact block relay path verifies epoch continuity but never checks that a header's `compact_target` matches the epoch's consensus-required difficulty. Because `compact_to_target(0x20ffffff)` returns a valid, non-overflowing U256 target so large that virtually any hash satisfies PoW, and because `CompactBlock` messages are explicitly exempt from rate limiting on the assumption that PoW is the cost barrier, an attacker can flood the unbounded `pending_compact_blocks` map with zero mining work, causing unbounded memory growth and lock contention on any reachable CKB node.

## Finding Description
**Root cause — missing `compact_target` check in relay `EpochVerifier`:**

`contextual_check` in `compact_block_process.rs` (line 324) constructs a `HeaderVerifier` and calls `verify()`. The `HeaderVerifier::verify` implementation (lines 32–50 of `header_verifier.rs`) delegates epoch validation to `EpochVerifier::new(parent_fields.epoch, header).verify()`. That `EpochVerifier` (lines 123–148 of `header_verifier.rs`) only checks `is_well_formed()` and `is_successor_of(parent)` — it has no access to the epoch's `compact_target` and never enforces it.

The `EpochVerifier` in `contextual_block_verifier.rs` (lines 500–507) does enforce `compact_target`, but it is only reached during full block acceptance, not during relay header verification.

**Step 1 — trivial PoW bypass:**

`compact_to_target(0x20ffffff)`: exponent = 32, mantissa = 0xffffff. Since `exponent (32) > 32` is false, `overflow = false`. The result is `0xffffff << (8 * 29)` = `0xffffff << 232`, a valid non-zero U256 near `2^256`. The guard at `eaglesong_blake2b.rs` line 22 (`block_target.is_zero() || overflow`) passes. The comparison at line 30 (`output > block_target`) fails for virtually all nonces, so PoW passes with probability ≈ `1 - 1/2^24`.

**Step 2 — rate limit exemption:**

`sync/src/relayer/mod.rs` lines 112–114 explicitly skip rate limiting for all `CompactBlock` messages with the comment: *"CompactBlock will be verified by POW, it's OK to skip rate limit checking."* This assumption is broken when `compact_target` is self-declared.

**Step 3 — unbounded map insertion:**

`pending_compact_blocks` is initialized as a plain `HashMap::default()` (line 1022 of `sync/src/types/mod.rs`) with no capacity bound. When reconstruction fails because short_ids don't match the tx pool, `missing_or_collided_post_process` (lines 354–361 of `compact_block_process.rs`) inserts the compact block into this map. The only eviction is epoch-based, triggered only when a legitimate block is accepted.

**Exploit flow:**
1. Attacker connects as a P2P peer and fetches the current tip header.
2. Crafts compact blocks: `compact_target=0x20ffffff`, valid parent hash, valid epoch succession, valid timestamp, nonce=0, plus short_ids absent from the target's tx pool.
3. Sends at full network speed — no rate limiter applies.
4. Each block passes `non_contextual_check`, `contextual_check` (PoW passes, epoch continuity passes, `compact_target` never checked), and `CompactBlockVerifier`.
5. Reconstruction fails (missing short_ids), so each block enters `pending_compact_blocks` via `missing_or_collided_post_process`.
6. The map grows without bound; each insertion also holds the `tokio::sync::Mutex` guarding the map.

## Impact Explanation
An unprivileged attacker with a single P2P connection can cause unbounded memory growth on any reachable CKB node, eventually crashing it via OOM. The `tokio::sync::Mutex` on `pending_compact_blocks` is held during PoW verification of each incoming block, creating async task starvation as the map grows. This matches the **High** impact: *"Vulnerabilities which could easily crash a CKB node."*

## Likelihood Explanation
The attack requires only a P2P connection and knowledge of the current tip (available via normal sync). No hashpower, private keys, or privileged access are needed. The rate-limit exemption removes the only practical throttle. The attack is repeatable and scalable — a single attacker can sustain it indefinitely with minimal CPU cost (one Eaglesong hash per block, which passes trivially).

## Recommendation
1. **Add `compact_target` validation to the relay-path `HeaderVerifier` or `contextual_check`**: derive the expected `compact_target` from the parent's epoch extension and reject headers that deviate. The epoch extension is accessible via `shared.consensus()` or the active chain snapshot.
2. **Remove or scope the rate-limit exemption for `CompactBlock`**: the exemption is only safe if PoW is enforced against the consensus difficulty. Until fix (1) is in place, apply the same rate limiter to `CompactBlock` as to other relay messages.
3. **Add a capacity bound to `pending_compact_blocks`**: use LRU eviction or a fixed maximum size (e.g., 512 entries) with timestamp-based expiry, independent of legitimate block acceptance.

## Proof of Concept
```
1. Connect to a CKB mainnet/testnet node as a P2P peer.
2. Fetch tip header T (number N, hash H, epoch E).
3. Construct a RawHeader:
     parent_hash    = H
     number         = N+1
     compact_target = 0x20ffffff   # compact_to_target → (2^256-2^232, false)
     epoch          = valid successor of E (e.g., same epoch, index+1)
     timestamp      = median_time(T) + 1
     nonce          = 0            # passes PoW with ~99.9999% probability
4. Wrap in a CompactBlock with:
     - one prefilled cellbase transaction (required by CompactBlockVerifier)
     - one short_id that does not exist in the target node's tx pool
5. Send via RelayMessage::CompactBlock over P2P.
6. Observe: HeaderVerifier passes (PoW ok, epoch continuity ok, compact_target unchecked).
            Block enters pending_compact_blocks via missing_or_collided_post_process.
7. Repeat with incrementing nonce/timestamp to generate unique block hashes.
8. Assert: pending_compact_blocks grows without bound; node RSS increases monotonically.
```

**Key code references:**

- `compact_to_target` overflow check: [1](#0-0) 
- PoW guard (passes for `0x20ffffff`): [2](#0-1) 
- Relay-path `EpochVerifier` (no `compact_target` check): [3](#0-2) 
- `contextual_check` uses relay-path `HeaderVerifier`: [4](#0-3) 
- Contextual `EpochVerifier` (does check `compact_target`, but not reached here): [5](#0-4) 
- Rate-limit exemption for `CompactBlock`: [6](#0-5) 
- `pending_compact_blocks` unbounded `HashMap`: [7](#0-6) 
- Insertion into map on reconstruction failure: [8](#0-7)

### Citations

**File:** util/types/src/utilities/difficulty.rs (L75-76)
```rust
    let overflow = !mantissa.is_zero() && (exponent > 32);
    (ret, overflow)
```

**File:** pow/src/eaglesong_blake2b.rs (L20-28)
```rust
        let (block_target, overflow) = compact_to_target(header.raw().compact_target().into());

        if block_target.is_zero() || overflow {
            debug!(
                "compact_target is invalid: {:#x}",
                header.raw().compact_target()
            );
            return false;
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

**File:** sync/src/relayer/compact_block_process.rs (L321-339)
```rust
    let median_time_context = CompactBlockMedianTimeView {
        fn_get_pending_header: Box::new(fn_get_pending_header),
    };
    let header_verifier = HeaderVerifier::new(&median_time_context, shared.consensus());
    if let Err(err) = header_verifier.verify(compact_block_header) {
        if err
            .downcast_ref::<HeaderError>()
            .map(|e| e.is_too_new())
            .unwrap_or(false)
        {
            return Status::ignored();
        } else {
            shared
                .shared()
                .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
            return StatusCode::CompactBlockHasInvalidHeader
                .with_context(format!("{block_hash} {err}"));
        }
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L354-361)
```rust
    shared
        .state()
        .pending_compact_blocks()
        .await
        .entry(block_hash.clone())
        .or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))
        .1
        .insert(peer, (missing_transactions.clone(), missing_uncles.clone()));
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

**File:** sync/src/types/mod.rs (L1022-1022)
```rust
            pending_compact_blocks: tokio::sync::Mutex::new(HashMap::default()),
```
