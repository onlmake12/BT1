### Title
Compact Block Staleness Pre-Check Uses `epoch_length` Instead of `start_number`, Allowing Peers to Bypass the Stale Filter — (File: `sync/src/relayer/compact_block_process.rs`)

---

### Summary

In `non_contextual_check()`, the staleness guard for incoming compact blocks computes the lowest acceptable block number by subtracting `epoch_length` (the *count* of blocks in the current epoch) from the tip block number. The correct variable is `start_number` (the *starting block number* of the current epoch). These are two distinct fields of `EpochExt`. The confusion causes the filter to accept compact blocks from the previous epoch, allowing any unprivileged peer to push stale blocks through the full reconstruction and verification pipeline.

---

### Finding Description

**Location:** `sync/src/relayer/compact_block_process.rs`, lines 211–220

```rust
// Only accept blocks with a height greater than tip - N
// where N is the current epoch length
let block_hash = header.hash();
let tip = active_chain.tip_header();
let epoch_length = active_chain.epoch_ext().length();
let lowest_number = tip.number().saturating_sub(epoch_length);

if lowest_number > header.number() {
    return StatusCode::CompactBlockIsStaled.with_context(block_hash);
}
```

**Error 1 — Wrong variable used.**

`EpochExt` has two distinct fields:

| Field | Meaning |
|---|---|
| `length` | Number of blocks in this epoch (a *count*, e.g. 1000) |
| `start_number` | Block number at which this epoch began (an *offset*, e.g. 5000) | [1](#0-0) 

The comment in `execute()` states the intent: *"use epoch as the judgment condition because we accept all block in current epoch as uncle block."* [2](#0-1) 

To accept only blocks within the current epoch, the correct lower bound is `epoch_ext().start_number()`, not `tip.number() - epoch_ext().length()`.

**Concrete divergence.** Suppose epoch 5 starts at block 5000 with length 1000, and the tip is at block 5200:

- Current check: `lowest_number = 5200 − 1000 = 4200`. Blocks ≥ 4200 pass — including blocks 4200–4999 from epoch 4.
- Correct check: `lowest_number = start_number = 5000`. Only blocks ≥ 5000 (epoch 5) pass.

The window of incorrectly accepted stale blocks is `[tip.number() − epoch_length, start_number − 1]`, which can be up to `epoch_length` blocks wide (widest at the start of a new epoch). [3](#0-2) 

**Error 2 — The check is partially redundant.**

The full verification pipeline — specifically `EpochVerifier` in `contextual_block_verifier.rs` — will ultimately reject any block whose epoch field does not match the expected epoch. So the stale pre-check is a performance optimisation, not a security gate. A block that slips past the wrong pre-check will be caught later, but only after the node has already paid the cost of compact block reconstruction (including potentially fetching missing transactions from the peer). [4](#0-3) 

---

### Impact Explanation

An unprivileged peer can send compact block messages whose `header.number` falls in the range `[tip.number() − epoch_length, epoch_start_number − 1]`. These blocks pass the stale filter and proceed to:

1. `contextual_check` — header status lookups and parent resolution.
2. `CompactBlockVerifier::verify` — structural checks.
3. `reconstruct_block` — may trigger outbound `BlockTransactions` requests to the peer, consuming bandwidth.
4. `accept_block` — full block verification, where the epoch mismatch is finally caught.

The attacker can repeat this for every block number in the window (up to `epoch_length` distinct values per epoch, typically ~1000 on mainnet), forcing the node to perform repeated reconstruction attempts and outbound fetches. This is a resource-exhaustion / DoS vector reachable by any peer without any privilege.

---

### Likelihood Explanation

The compact block relay protocol is open to all connected peers. No authentication or privilege is required to send a `CompactBlock` message. The window of exploitable block numbers is largest at the beginning of each epoch (up to `epoch_length` blocks wide) and shrinks as the epoch progresses. On mainnet, epoch lengths are on the order of 1000–1800 blocks, giving a persistent and predictable attack surface throughout each epoch.

---

### Recommendation

Replace `epoch_length` with `start_number` in the staleness check:

```rust
// Only accept blocks within the current epoch
let epoch_start = active_chain.epoch_ext().start_number();
if header.number() < epoch_start {
    return StatusCode::CompactBlockIsStaled.with_context(block_hash);
}
```

`EpochExt::start_number()` is already available on the same object and directly encodes the intended lower bound. [5](#0-4) 

---

### Proof of Concept

Assume mainnet state: epoch 5 starts at block 5000, length = 1000, current tip = block 5001 (just entered epoch 5).

1. Attacker constructs a compact block header with `number = 4100` (epoch 4, well before epoch 5).
2. Current check: `lowest_number = 5001 − 1000 = 4001`. Since `4001 ≤ 4100`, the block **passes** the stale filter.
3. Correct check: `4100 < 5000` → block **should be rejected** as stale.
4. The node proceeds to `contextual_check`, `CompactBlockVerifier::verify`, and `reconstruct_block`. If the compact block lists missing transactions, the node sends a `GetBlockTransactions` request back to the attacker.
5. The attacker can repeat this for all block numbers in `[4001, 4999]` — 999 distinct stale blocks — each triggering a reconstruction attempt and potential outbound fetch, before the epoch mismatch is caught in `EpochVerifier`. [6](#0-5)

### Citations

**File:** util/types/src/core/extras.rs (L93-104)
```rust
/// Extended epoch information.
#[derive(Clone, Eq, PartialEq, Debug, Default)]
pub struct EpochExt {
    pub(crate) number: EpochNumber,
    pub(crate) base_block_reward: Capacity,
    pub(crate) remainder_reward: Capacity,
    pub(crate) previous_epoch_hash_rate: U256,
    pub(crate) last_block_hash_in_previous_epoch: packed::Byte32,
    pub(crate) start_number: BlockNumber,
    pub(crate) length: BlockNumber,
    pub(crate) compact_target: u32,
}
```

**File:** util/types/src/core/extras.rs (L145-148)
```rust
    /// Returns the starting block number of this epoch.
    pub fn start_number(&self) -> BlockNumber {
        self.start_number
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L108-116)
```rust
                // remove all pending request below this block epoch
                //
                // use epoch as the judgment condition because we accept
                // all block in current epoch as uncle block
                pending_compact_blocks.retain(|_, (v, _, _)| {
                    Into::<EpochNumberWithFraction>::into(v.header().as_reader().raw().epoch())
                        .number()
                        >= block.epoch().number()
                });
```

**File:** sync/src/relayer/compact_block_process.rs (L190-223)
```rust
fn non_contextual_check(
    compact_block: &CompactBlock,
    header: &HeaderView,
    consensus: &Consensus,
    active_chain: &ActiveChain,
) -> Status {
    if compact_block.uncles().len() > consensus.max_uncles_num() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "CompactBlock uncles count({}) > consensus max_uncles_num({})",
            compact_block.uncles().len(),
            consensus.max_uncles_num()
        ));
    }
    if (compact_block.proposals().len() as u64) > consensus.max_block_proposals_limit() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "CompactBlock proposals count({}) > consensus max_block_proposals_limit({})",
            compact_block.proposals().len(),
            consensus.max_block_proposals_limit(),
        ));
    }

    // Only accept blocks with a height greater than tip - N
    // where N is the current epoch length
    let block_hash = header.hash();
    let tip = active_chain.tip_header();
    let epoch_length = active_chain.epoch_ext().length();
    let lowest_number = tip.number().saturating_sub(epoch_length);

    if lowest_number > header.number() {
        return StatusCode::CompactBlockIsStaled.with_context(block_hash);
    }

    Status::ok()
}
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L488-509)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let header = self.block.header();
        let actual_epoch_with_fraction = header.epoch();
        let block_number = header.number();
        let epoch_with_fraction = self.epoch.number_with_fraction(block_number);
        if actual_epoch_with_fraction != epoch_with_fraction {
            return Err(EpochError::NumberMismatch {
                expected: epoch_with_fraction.full_value(),
                actual: actual_epoch_with_fraction.full_value(),
            }
            .into());
        }
        let actual_compact_target = header.compact_target();
        if self.epoch.compact_target() != actual_compact_target {
            return Err(EpochError::TargetMismatch {
                expected: self.epoch.compact_target(),
                actual: actual_compact_target,
            }
            .into());
        }
        Ok(())
    }
```
