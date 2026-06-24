Audit Report

## Title
Missing `compact_target` Epoch Validation in Compact Block Relay Header Verifier Enables Unbounded `pending_compact_blocks` Flooding — (`sync/src/relayer/compact_block_process.rs`, `verification/src/header_verifier.rs`)

## Summary

The `HeaderVerifier` used in the compact block relay path (`contextual_check`) verifies PoW only against the header's self-declared `compact_target`, without checking that `compact_target` matches the epoch's consensus-mandated difficulty. Because `compact_to_target(0x20ffffff)` produces a near-maximum 256-bit target with no overflow, any nonce satisfies the PoW check with ~99.99% probability. An unprivileged P2P peer can craft compact blocks with `compact_target=0x20ffffff`, pass all relay-path header checks, and insert entries into the unbounded `pending_compact_blocks` map at essentially zero cost, exhausting node memory.

## Finding Description

**Root cause — `EpochVerifier` in relay path omits `compact_target` check:**

`HeaderVerifier::verify` runs four sub-verifiers: `PowVerifier`, `NumberVerifier`, `EpochVerifier`, and `TimestampVerifier`. The relay-path `EpochVerifier` only checks `is_well_formed()` and `is_successor_of(parent)` — it never validates that the header's `compact_target` matches the epoch's mandated target. [1](#0-0) 

**`PowVerifier` accepts `compact_target=0x20ffffff`:**

`compact_to_target(0x20ffffff)` yields exponent=32, mantissa=0xffffff, `ret = 0xffffff << 232`, and `overflow = !mantissa.is_zero() && (32 > 32) = false`. The target is non-zero and no overflow, so `EaglesongBlake2bPowEngine::verify` passes the guard. With `block_target = 0xffffff << 232`, any hash whose top 24 bits are ≤ 0xffffff satisfies `output <= block_target` — approximately 99.9999% of all hashes, meaning nonce=0 passes with overwhelming probability. [2](#0-1) [3](#0-2) 

**Relay path uses `HeaderVerifier` without `compact_target` enforcement:**

`contextual_check` constructs a `HeaderVerifier` and calls `verify`. If it returns `Ok`, the compact block proceeds to insertion. [4](#0-3) 

**Insertion into unbounded `pending_compact_blocks`:**

After `contextual_check` returns `Status::ok()`, `missing_or_collided_post_process` inserts the compact block into `pending_compact_blocks`, which is a plain `HashMap` with no size cap. The only cleanup is an epoch-based `retain` triggered when a legitimate block is accepted — an attacker flooding the map prevents this cleanup from being effective. [5](#0-4) [6](#0-5) 

**The `compact_target` check exists only in the contextual full-block verifier:**

The contextual `EpochVerifier` in `verification/contextual/src/contextual_block_verifier.rs` does enforce `compact_target == epoch.compact_target()`, but this is only reached when a fully-reconstructed block is submitted to the chain — never for a pending compact block awaiting transactions. [7](#0-6) 

**`CompactBlockIsAlreadyPending` deduplication is insufficient:**

The deduplication check only prevents the same `(block_hash, peer)` pair from being re-inserted. An attacker varying nonce, timestamp, or other header fields produces distinct block hashes, each of which passes independently. [8](#0-7) 

## Impact Explanation

An attacker with only a P2P connection can flood `pending_compact_blocks` with arbitrarily many entries at near-zero cost. Each entry holds a `CompactBlock` struct plus associated metadata. With no size limit or time-based eviction on the map, this constitutes unbounded memory growth leading to OOM crash of the victim node. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points). It also qualifies as **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, since the attack can be applied to multiple nodes simultaneously.

## Likelihood Explanation

The attack requires only: (1) a P2P connection to a CKB node, (2) knowledge of the current tip block hash and epoch (publicly observable), and (3) trivial CPU to compute a valid nonce (nonce=0 works ~99.99% of the time). No hashpower, no privileged access, and no special knowledge are required. The attack is fully repeatable and can be automated to sustain continuous flooding.

## Recommendation

In `contextual_check` (or in `HeaderVerifier::verify`), add a check that the header's `compact_target` matches the epoch's consensus-mandated `compact_target`. The parent's `EpochExt` is resolvable from the parent block already fetched during verification. Mirror the check already present in the contextual verifier:

```rust
if self.epoch.compact_target() != header.compact_target() {
    return Err(EpochError::TargetMismatch {
        expected: self.epoch.compact_target(),
        actual: header.compact_target(),
    }.into());
}
```

This check should be added to the relay-path `EpochVerifier` in `verification/src/header_verifier.rs`, requiring the `EpochExt` (or at minimum its `compact_target`) to be passed through `HeaderFields` or resolved inline in `contextual_check` after the parent is fetched.

## Proof of Concept

1. Connect to a CKB mainnet/testnet node via P2P relay protocol.
2. Query the current tip: obtain `tip_hash`, `tip_number`, and `tip_epoch` (all public).
3. Construct a `RawHeader` with:
   - `parent_hash = tip_hash`
   - `number = tip_number + 1`
   - `epoch = valid successor EpochNumberWithFraction of tip_epoch`
   - `compact_target = 0x20ffffff`
   - `timestamp = median_time_of_tip + 1` (satisfies `TimestampVerifier`)
4. Set `nonce = 0`. Compute `eaglesong(blake2b(pow_message(pow_hash, 0)))`. With ~99.99% probability the output satisfies `output <= 0xffffff << 232`.
5. Wrap in a `CompactBlock` message with one prefilled cellbase transaction and no short IDs (forcing `ReconstructionResult::Missing`).
6. Send via the relay protocol.
7. Observe the node does not return `CompactBlockHasInvalidHeader`; the block hash appears in `pending_compact_blocks`.
8. Repeat with `timestamp += 1` (or varying nonce) to generate distinct block hashes. Each iteration inserts a new entry into `pending_compact_blocks`.
9. Monitor victim node memory growth until OOM or service degradation.

### Citations

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

**File:** sync/src/relayer/compact_block_process.rs (L283-291)
```rust
    // compact block is in pending
    let pending_compact_blocks = shared.state().pending_compact_blocks().await;
    if pending_compact_blocks
        .get(&block_hash)
        .map(|(_, peers_map, _)| peers_map.contains_key(&peer))
        .unwrap_or(false)
    {
        return StatusCode::CompactBlockIsAlreadyPending.with_context(block_hash);
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
