All seven cited code locations have been verified against the actual repository. Every claim checks out exactly as described.

---

Audit Report

## Title
CompactBlock Rate Limiter Bypass via Arbitrary `compact_target` — Missing Epoch Target Validation in Relay-Path `HeaderVerifier` — (`sync/src/relayer/compact_block_process.rs`, `verification/src/header_verifier.rs`)

## Summary
The relay path explicitly exempts `CompactBlock` messages from the per-peer rate limiter, relying on PoW cost as the sole throttle. `HeaderVerifier::verify`, the only header verifier invoked in the relay path, never checks that the `compact_target` embedded in the header matches the expected epoch target. An attacker can embed an arbitrarily easy `compact_target` (e.g., the testnet genesis value `0x1e015555`), mine valid nonces at negligible CPU cost, and flood any node with an unbounded stream of `CompactBlock` messages that each pass all relay-path checks and trigger full compact-block reconstruction logic.

## Finding Description
**Rate limiter bypass:** `Relayer::try_process` sets `should_check_rate = false` for every `CompactBlock` message, with an explicit developer comment that PoW is the intended cost barrier. [1](#0-0) 

**Missing compact_target validation:** `HeaderVerifier::verify` invokes `PowVerifier`, `NumberVerifier`, `EpochVerifier`, and `TimestampVerifier` — no check compares the header's `compact_target` against the network-expected epoch target. [2](#0-1) 

`EpochVerifier::verify` checks only that the epoch field is well-formed and is a valid successor of the parent's epoch. It has no access to the expected `compact_target` and performs no such check. [3](#0-2) 

`PowVerifier` calls `compact_to_target(header.raw().compact_target())`, using the value the attacker placed in the header, not the network-expected value. [4](#0-3) 

**compact_target check is relay-path-absent:** The `EpochError::TargetMismatch` check — which compares `self.epoch.compact_target()` against `header.compact_target()` — exists only in `contextual_block_verifier.rs` and is invoked only during full block acceptance after reconstruction, not during the relay-path `contextual_check`. [5](#0-4) 

**Exploit flow:**
1. Attacker connects to a target node over P2P.
2. Attacker reads the current tip hash and epoch (publicly available via RPC or P2P).
3. Attacker constructs a header with `parent_hash = tip_hash`, `number = tip + 1`, `epoch = valid_successor`, `timestamp = now`, and `compact_target = 0x1e015555` (testnet genesis value, confirmed). [6](#0-5) 
4. Attacker mines a valid nonce against the trivial target in microseconds per block.
5. Attacker sends `RelayMessage::CompactBlock`. No rate limit fires.
6. Node executes `non_contextual_check` (passes), then `contextual_check` (passes: `HeaderVerifier` passes because PoW satisfies the attacker-chosen target and epoch continuity holds), then `CompactBlockVerifier::verify`, then `reconstruct_block` with tx-pool lookups. [7](#0-6) 
7. Each crafted block has a unique hash (different nonce), so block-status and pending-dedup guards never fire.
8. Loop indefinitely with no per-peer message cap.

## Impact Explanation
Every received message causes two sequential hash computations (eaglesong → blake2b_256), tx-pool lookups, and compact-block reconstruction logic on the relay processing thread, with no bound on message rate per peer. A single attacker connection can saturate the relay thread, delaying or blocking propagation of legitimate blocks. This matches the allowed **High** impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
The attack requires only a standard P2P connection to a testnet node, knowledge of the current tip (publicly available), and a CPU loop mining against `compact_target = 0x1e015555`. No privileged access, no hashpower majority, and no key material is required. The rate-limiter bypass is intentional and documented in source. The missing `compact_target` check in `HeaderVerifier` is a concrete, confirmed code gap. The attack is repeatable indefinitely and requires no victim interaction.

## Recommendation
1. **Add compact_target validation to the relay-path `HeaderVerifier`:** The `EpochVerifier` in `header_verifier.rs` should receive the expected `compact_target` (derivable from the parent's epoch extension via `epoch_ext.compact_target()`) and reject headers whose embedded `compact_target` does not match before any PoW computation.
2. **Reject mismatched compact_target before PoW:** Checking `compact_target` against the expected epoch value is a cheap integer comparison; it should precede the eaglesong hash computation to eliminate the CPU cost entirely for crafted messages.
3. **Apply a per-peer rate cap to CompactBlock messages as a defense-in-depth measure:** Even with correct `compact_target` validation, a low cap (e.g., 2–5/sec) bounds damage from any future bypass.

## Proof of Concept
```python
# Pseudocode — runs on commodity hardware, finds valid nonce in microseconds per iteration
tip_hash   = rpc_get_tip_hash()
tip_number = rpc_get_tip_number()
parent_epoch = rpc_get_tip_epoch()          # e.g., epoch 42, index 100, length 1800
next_epoch   = successor_epoch(parent_epoch) # epoch 42, index 101, length 1800

while True:
    nonce = random_u128()
    header = build_header(
        parent_hash    = tip_hash,
        number         = tip_number + 1,
        compact_target = 0x1e015555,        # trivial target; no relay-path check rejects this
        epoch          = next_epoch,         # passes EpochVerifier continuity check
        timestamp      = now_ms(),           # passes TimestampVerifier
        nonce          = nonce,
    )
    pow_input  = pow_message(calc_pow_hash(header), nonce)
    pow_output = blake2b_256(eaglesong(pow_input))
    if U256(pow_output) <= compact_to_target(0x1e015555):
        compact_block = build_compact_block(header, prefilled_txs=[coinbase])
        p2p_send(RelayMessage::CompactBlock(compact_block))
        # Node executes full CompactBlockProcess::execute with no rate limit applied
        # Each iteration: unique hash → dedup guards do not fire → full processing path reached
```
The inner loop finds a valid nonce in microseconds. The victim node processes each message through the full `CompactBlockProcess::execute` path with no per-peer message bound.

### Citations

**File:** sync/src/relayer/mod.rs (L112-114)
```rust
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));
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

**File:** resource/specs/testnet.toml (L7-7)
```text
compact_target = 0x1e015555
```

**File:** sync/src/relayer/compact_block_process.rs (L56-93)
```rust
    pub async fn execute(self) -> Status {
        let instant = Instant::now();
        let shared = self.relayer.shared();
        let active_chain = shared.active_chain();
        let compact_block = self.message.to_entity();
        let header = compact_block.header().into_view();
        let block_hash = header.hash();

        let status =
            non_contextual_check(&compact_block, &header, shared.consensus(), &active_chain);
        if !status.is_ok() {
            return status;
        }

        let status = contextual_check(&header, shared, &active_chain, &self.nc, self.peer).await;
        if !status.is_ok() {
            return status;
        }

        // The new arrived has greater difficulty than local best known chain
        attempt!(CompactBlockVerifier::verify(&compact_block));
        // Header has been verified ok, update state
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
```
