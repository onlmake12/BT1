The claim requires careful verification of each step. Let me trace through the actual code.

**Step 1: `compact_to_target(0x20ffffff)` behavior**

From `util/types/src/utilities/difficulty.rs`:
- exponent = `0x20ffffff >> 24 = 32`
- mantissa = `0xffffff`
- overflow = `!mantissa.is_zero() && (exponent > 32)` = `true && false` = **false**
- result = `0xffffff << (8 * (32-3))` = `0xffffff << 232` = `2^256 - 2^232`

The question slightly misstates this as `U256::max_value()`, but the actual result is `2^256 - 2^232` — a valid, non-overflowing U256 value that is an enormous target. The `block_target.is_zero() || overflow` guard at line 22 passes. [1](#0-0) 

**Step 2: PoW check with this target**

`output > block_target` fails only if the hash's top 24 bits are all `0xff` AND any lower bit is set — probability ≈ `1/2^24`. So nonce=0 passes with ~99.9999% probability. [2](#0-1) 

**Step 3: `HeaderVerifier::EpochVerifier` in the relay path**

The `EpochVerifier` used by `HeaderVerifier` (in `verification/src/header_verifier.rs`) takes only `parent: EpochNumberWithFraction` — it checks epoch continuity and well-formedness, but **does not check `compact_target` against the epoch's consensus value**. [3](#0-2) 

The contextual `EpochVerifier` in `contextual_block_verifier.rs` (lines 500–507) *does* check `compact_target`, but it is only invoked during full block processing, not during compact block relay header verification. [4](#0-3) 

**Step 4: Rate limiting is explicitly disabled for CompactBlock**

The relay's `try_process` explicitly skips rate limiting for `CompactBlock` messages, with the comment "CompactBlock will be verified by POW, it's OK to skip rate limit checking." This design assumption is broken when `compact_target` can be self-declared. [5](#0-4) 

**Step 5: `pending_compact_blocks` is unbounded**

The map is initialized as a plain `HashMap::default()` with no capacity limit. The only cleanup is epoch-based eviction when a legitimate block is accepted. An attacker sending compact blocks with unresolvable short_ids causes them to accumulate indefinitely. [6](#0-5) [7](#0-6) 

**Step 6: The `missing_or_collided_post_process` insertion path**

If the attacker includes short_ids that don't match the tx pool, reconstruction fails and the compact block is inserted into `pending_compact_blocks`. [8](#0-7) 

---

### Title
Missing `compact_target` Epoch Validation in Compact Block Relay Header Verifier Enables Zero-Work DoS — (`sync/src/relayer/compact_block_process.rs`, `verification/src/header_verifier.rs`)

### Summary
The `HeaderVerifier` used in the compact block relay path does not verify that a header's `compact_target` matches the epoch's consensus-required difficulty. Combined with the explicit rate-limit exemption for `CompactBlock` messages (justified by the assumption that PoW is the guard), an attacker can relay compact blocks with `compact_target=0x20ffffff` and any nonce, trivially passing PoW, and flood the unbounded `pending_compact_blocks` map.

### Finding Description
`compact_to_target(0x20ffffff)` returns `(2^256 - 2^232, false)` — a valid, non-overflowing target so large that virtually any hash satisfies `output <= block_target`. The `EaglesongBlake2bPowEngine::verify` guard at line 22 (`block_target.is_zero() || overflow`) passes, and the comparison at line 30 passes for ~`(1 - 1/2^24)` of all nonces.

The `EpochVerifier` inside `HeaderVerifier` (used in `contextual_check` at line 324) only holds `parent: EpochNumberWithFraction` and checks epoch continuity — it has no access to the epoch's `compact_target` and cannot enforce it. The full contextual `EpochVerifier` that does check `compact_target` (in `contextual_block_verifier.rs`) is only reached during full block acceptance, not during relay header verification.

`CompactBlock` messages are explicitly exempt from the 30-req/s rate limiter because PoW is assumed to be the cost barrier. With a self-declared minimum-difficulty target, this barrier is eliminated.

### Impact Explanation
An attacker connected as a P2P peer can:
1. Obtain the current tip header via normal sync.
2. Craft compact blocks with `compact_target=0x20ffffff`, valid parent hash, valid epoch succession, valid timestamp, and nonce=0 (or any value), plus short_ids that don't match the tx pool.
3. Relay them at full network speed (no rate limit).
4. Each block passes `non_contextual_check`, `contextual_check` (including PoW), and `CompactBlockVerifier`, then enters `pending_compact_blocks` via `missing_or_collided_post_process`.
5. The map grows without bound, consuming memory and causing lock contention on the `tokio::sync::Mutex` that guards it (held during PoW verification of each incoming block).

Impact: unbounded memory growth and async task starvation on any reachable CKB node, achievable with zero mining work.

### Likelihood Explanation
The attack requires only a P2P connection and knowledge of the current tip (available via normal sync). No hashpower, keys, or privileged access are needed. The rate-limit exemption removes the only practical throttle.

### Recommendation
1. Add `compact_target` validation to the relay-path `HeaderVerifier` or `contextual_check`: derive the expected `compact_target` from the parent's epoch and reject headers that deviate.
2. Remove or scope the rate-limit exemption for `CompactBlock` — the exemption is only safe if PoW is enforced against the consensus difficulty, not a self-declared one.
3. Add a capacity bound to `pending_compact_blocks` with LRU or timestamp-based eviction.

### Proof of Concept
```
1. Connect to a CKB mainnet/testnet node as a P2P peer.
2. Fetch tip header T (number N, hash H, epoch E).
3. Construct a RawHeader:
     parent_hash    = H
     number         = N+1
     compact_target = 0x20ffffff
     epoch          = valid successor of E (e.g., E.index+1 / E.length)
     timestamp      = median_time(T) + 1
     nonce          = 0
4. Wrap in a CompactBlock with one prefilled cellbase tx and
   one short_id that does not exist in the target's tx pool.
5. Send via RelayMessage::CompactBlock over P2P.
6. Observe: HeaderVerifier passes, block enters pending_compact_blocks.
7. Repeat with incrementing nonce/timestamp to generate unique hashes.
8. Assert: pending_compact_blocks grows without bound; node memory increases.
```

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

**File:** sync/src/types/mod.rs (L979-987)
```rust
// <CompactBlockHash, (CompactBlock, <PeerIndex, (Vec<TransactionsIndex>, Vec<UnclesIndex>)>, timestamp)>
pub(crate) type PendingCompactBlockMap = HashMap<
    Byte32,
    (
        packed::CompactBlock,
        HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>,
        u64,
    ),
>;
```

**File:** sync/src/types/mod.rs (L1022-1022)
```rust
            pending_compact_blocks: tokio::sync::Mutex::new(HashMap::default()),
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
