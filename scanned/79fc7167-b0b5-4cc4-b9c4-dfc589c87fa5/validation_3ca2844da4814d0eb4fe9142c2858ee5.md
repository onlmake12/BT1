The code evidence is conclusive. Here is the full analysis:

---

### Title
PoW-First Ordering in `HeaderVerifier::verify` Enables Zero-Cost CPU Exhaustion via `SendHeaders` Flood — (`verification/src/header_verifier.rs`, `sync/src/synchronizer/headers_process.rs`)

### Summary

`HeaderVerifier::verify` unconditionally runs the full EaglesongBlake2b PoW check before the parent-hash lookup. Because the `compact_target` field is attacker-controlled and is **not** validated against the expected epoch difficulty at the header-verification stage, an attacker can craft headers with minimum difficulty (`compact_target = 0x207fffff`), for which any nonce satisfies the PoW check. The Sync protocol's `SendHeaders` handler has **no rate limiter**. Headers that fail with `UnknownParent` are classified `TemporaryInvalid` and are **not** written to the block-status cache, so the same header can be re-submitted indefinitely. The result is sustained, zero-cost CPU exhaustion on every targeted node.

---

### Finding Description

**Step 1 — PoW runs before parent lookup, with no difficulty gate.** [1](#0-0) 

The comment `// POW check first` is literal. `PowVerifier::verify` calls `EaglesongBlake2bPowEngine::verify`, which runs `eaglesong` + `blake2b_256` and then compares the output against `compact_to_target(header.compact_target())`. [2](#0-1) 

The `compact_target` is taken directly from the attacker-supplied header. The only rejection is `block_target.is_zero() || overflow`. Setting `compact_target = 0x207fffff` produces a target of `0x7fffff << 232` — any 256-bit hash satisfies it, so any nonce is valid.

**Step 2 — The `EpochVerifier` in `HeaderVerifier` does NOT check `compact_target`.** [3](#0-2) 

This verifier only checks epoch-number continuity (`is_successor_of`). The `compact_target` vs. expected-epoch-difficulty check lives in the *contextual* block verifier's `EpochVerifier`, which is only reached for full blocks, never for header-only sync. [4](#0-3) 

**Step 3 — `TemporaryInvalid` (UnknownParent) is not cached.** [5](#0-4) 

When `non_contextual_check` returns `TemporaryInvalid` (the `UnknownParent` path), `is_invalid == false`, so `insert_block_status(…, BLOCK_INVALID)` is **not** called. The header is silently dropped. The next identical message triggers the full PoW computation again.

**Step 4 — No rate limiter on the Sync protocol's `SendHeaders` handler.** [6](#0-5) 

`Synchronizer::try_process` dispatches `SendHeaders` to `HeadersProcess::execute` with zero rate-limiting. Compare with the Relayer, which has a 30 req/s limiter — but even that explicitly exempts `CompactBlock` with the comment "CompactBlock will be verified by POW, it's OK to skip rate limit checking." [7](#0-6) 

**Step 5 — `MAX_HEADERS_LEN = 2000` per message.** [8](#0-7) 

If the attacker's first header has a known parent (e.g., genesis), all 2000 headers in the batch are PoW-verified before any difficulty check can reject them. If the first header has an unknown parent, `execute()` returns after 1 PoW verification — but the attacker simply sends a new message per header.

---

### Impact Explanation

Every `SendHeaders` message from an attacker causes at least one (and up to 2000) full `eaglesong` + `blake2b_256` computations on the victim node. The attacker's cost to generate each valid-PoW header at minimum difficulty is effectively zero (any nonce works). With no rate limiting on the Sync protocol and no caching of `TemporaryInvalid` results, the attacker can sustain this indefinitely across all connected peers, causing CPU exhaustion proportional to the number of attacker connections.

---

### Likelihood Explanation

The attack requires only a standard P2P connection — no privileged role, no hashpower, no key material. The attacker needs to: (1) connect to the target node, (2) craft headers with `compact_target = 0x207fffff` and any nonce, (3) send `SendHeaders` messages in a tight loop. All of this is achievable with a trivial script.

---

### Recommendation

1. **Check `compact_target` before running PoW.** Reject headers whose `compact_target` is weaker than a minimum threshold (e.g., the genesis epoch's target) before invoking `PowVerifier`.
2. **Cache `TemporaryInvalid` headers with a short TTL.** Prevent re-verification of the same header within a time window.
3. **Add a rate limiter to the Sync protocol's `SendHeaders` handler**, analogous to the Relayer's per-peer limiter.
4. **Reorder checks in `HeaderVerifier::verify`**: perform the cheapest rejection (parent lookup) before the expensive PoW computation, or at minimum validate that `compact_target` is within a plausible range first.

---

### Proof of Concept

```python
# Pseudocode — attacker loop
import ckb_p2p

node = ckb_p2p.connect("victim_node:8115")
genesis_hash = node.get_genesis_hash()

while True:
    header = craft_header(
        parent_hash=random_unknown_hash(),  # or genesis_hash for batch of 2000
        compact_target=0x207fffff,          # minimum difficulty: any nonce valid
        nonce=0,                            # nonce 0 satisfies compact_target=0x207fffff
        epoch=valid_epoch_successor(),
    )
    node.send_headers([header])             # triggers 1 eaglesong+blake2b on victim
    # no sleep needed — no rate limit on Sync protocol
```

Each iteration costs the attacker ~0 CPU (nonce 0 always works at minimum difficulty) and costs the victim one full `eaglesong` + `blake2b_256` invocation with no caching benefit.

### Citations

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

**File:** pow/src/eaglesong_blake2b.rs (L12-31)
```rust
    fn verify(&self, header: &Header) -> bool {
        let input = crate::pow_message(&header.as_reader().calc_pow_hash(), header.nonce().into());
        let output = {
            let mut output_tmp = [0u8; 32];
            eaglesong(&input, &mut output_tmp);
            blake2b_256(output_tmp)
        };

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

**File:** sync/src/synchronizer/headers_process.rs (L334-344)
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
```

**File:** sync/src/synchronizer/mod.rs (L381-422)
```rust
    async fn try_process(
        &self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::SyncMessageUnionReader<'_>,
    ) -> Status {
        let _trace_timecost: Option<HistogramTimer> = {
            ckb_metrics::handle().map(|handle| {
                handle
                    .ckb_sync_msg_process_duration
                    .with_label_values(&[message.item_name()])
                    .start_timer()
            })
        };

        match message {
            packed::SyncMessageUnionReader::GetHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    GetHeadersProcess::new(reader, self, peer, &nc).execute()
                })
            }
            packed::SyncMessageUnionReader::SendHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    HeadersProcess::new(reader, self, peer, &nc).execute()
                })
            }
            packed::SyncMessageUnionReader::GetBlocks(reader) => {
                tokio::task::block_in_place(|| {
                    GetBlocksProcess::new(reader, self, peer, &nc).execute()
                })
            }
            packed::SyncMessageUnionReader::SendBlock(reader) => {
                if reader.check_data() {
                    BlockProcess::new(reader, self, peer, nc).execute()
                } else {
                    StatusCode::ProtocolMessageIsMalformed.with_context("SendBlock is invalid")
                }
            }
            packed::SyncMessageUnionReader::InIBD(_) => {
                InIBDProcess::new(self, peer, &nc).execute().await
            }
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

**File:** util/constant/src/sync.rs (L8-8)
```rust
pub const MAX_HEADERS_LEN: usize = 2_000;
```
