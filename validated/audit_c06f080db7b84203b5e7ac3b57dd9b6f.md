All code references check out. Let me verify the exact line numbers and flow before rendering the final output.

The code confirms every claim in the report:

- [1](#0-0)  — `CompactBlock` is unconditionally excluded from the rate limiter; all other relay message types go through `check_key`.
- [2](#0-1)  — `eaglesong()` runs at line 14 before `compact_target` validity is checked at line 18 and before the output is compared to target at line 26.
- [3](#0-2)  — `PowVerifier` is the first check in `HeaderVerifier::verify`.
- [4](#0-3)  — `non_contextual_check` only gates on uncle count, proposals count, and block height vs tip; all trivially satisfied with 0 uncles, 0 proposals, and `height = tip + 1`.
- [5](#0-4)  — All early exits in `contextual_check` are keyed on `block_hash`; varying the nonce produces a fresh hash, bypassing `BLOCK_STORED`, `BLOCK_RECEIVED`, and `BLOCK_INVALID` caches.
- [6](#0-5)  — `BLOCK_INVALID` is written only after PoW fails, and only for that specific hash; the next message with a different nonce starts fresh.
- [7](#0-6)  — Banning is post-hoc, after `try_process` returns, allowing pipelined messages to be processed before the ban takes effect.

---

Audit Report

## Title
Unbounded CPU Exhaustion via Rate-Limit Exemption for CompactBlock Messages — (`sync/src/relayer/mod.rs`, `pow/src/eaglesong.rs`)

## Summary
`CompactBlock` messages are unconditionally exempted from the per-peer rate limiter in `sync/src/relayer/mod.rs`. Because `EaglesongPowEngine::verify` always executes the full `eaglesong()` hash computation before checking the result, and because all early-exit caches in `contextual_check` are keyed on block hash (bypassed by varying the nonce), an attacker can force repeated PoW computations and DB reads per connection with no application-layer throttle, constituting a High-severity denial-of-service path capable of causing CKB network congestion at low attacker cost.

## Finding Description

**Rate-limit bypass — confirmed in source**

`sync/src/relayer/mod.rs` lines 112–114 unconditionally skip the rate limiter for every `CompactBlock` message:

```rust
// CompactBlock will be verified by POW, it's OK to skip rate limit checking.
let should_check_rate =
    !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));
```

All other relay message types are gated by a per-`(PeerIndex, message_type)` governor limiter; `CompactBlock` is not.

**PoW runs unconditionally before the result is checked**

`pow/src/eaglesong.rs` `EaglesongPowEngine::verify` calls `eaglesong()` at line 14 before checking `compact_target` validity at line 18 and comparing the output to the target at line 26:

```rust
fn verify(&self, header: &Header) -> bool {
    let input = crate::pow_message(&header.as_reader().calc_pow_hash(), header.nonce().into());
    let mut output = [0u8; 32];
    eaglesong(&input, &mut output);          // line 14 — always executes
    let (block_target, overflow) = compact_to_target(...);  // line 16
    if block_target.is_zero() || overflow { return false; } // line 18
    if U256::from_big_endian(&output[..])... > block_target { return false; } // line 26
```

**PoW is the first check inside `HeaderVerifier::verify`**

`verification/src/header_verifier.rs` line 34 calls `PowVerifier` before any other check (number, epoch, timestamp):

```rust
PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
```

**`non_contextual_check` is trivially satisfied**

The only pre-`contextual_check` gate checks uncle count ≤ `max_uncles_num`, proposals count ≤ `max_block_proposals_limit`, and block height ≥ `tip − epoch_length`. All three are trivially satisfied by a crafted header with 0 uncles, 0 proposals, and `height = tip + 1`.

**`contextual_check` cache is bypassed by varying the nonce**

`contextual_check` has early exits for `BLOCK_STORED`, `BLOCK_RECEIVED`, `BLOCK_INVALID`, missing parent, and already-pending-for-this-peer — all keyed on the **block hash** (lines 241, 256, 259, 285–291). An attacker who varies the nonce field produces a fresh hash on every message, bypassing every cache. After PoW fails, the hash is marked `BLOCK_INVALID` at lines 333–335, but the next message with a different nonce starts fresh.

**Peer banning is post-hoc and pipelineable**

After `try_process` returns `CompactBlockHasInvalidHeader`, `process()` at lines 195–204 calls `nc.ban_peer()`. However, banning occurs only after the message is fully processed. In an async network stack, an attacker can pipeline a burst of `CompactBlock` messages before the first ban takes effect, obtaining multiple `eaglesong()` computations per connection. The attacker can also reconnect from different IPs after each ban.

## Impact Explanation

Per malicious message the victim node executes: one `eaglesong()` hash computation (CPU-bound), `active_chain.get_block_status()` (DB read), `shared.get_header_index_view()` (DB read for parent), and `pending_compact_blocks().await` (async lock acquisition). With no rate limit on `CompactBlock`, a burst of pipelined messages per connection and/or multiple connections from different IPs can sustain elevated CPU and I/O load. This matches the allowed High-severity impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation

The attack requires only a standard TCP connection to a synced node. The attacker needs one valid parent hash (publicly observable from the P2P network or RPC). Crafting headers with varying nonces is trivial — no PoW solution is required. The exemption is unconditional and applies to all peers regardless of reputation. While per-IP banning limits sustained single-connection attacks, the attacker can pipeline messages before the ban and rotate IPs cheaply (VPS, botnet, Tor exit nodes), making the attack repeatable at low cost.

## Recommendation

Remove the blanket rate-limit exemption for `CompactBlock`. The developer comment ("CompactBlock will be verified by POW") conflates *solving* PoW with *verifying* PoW — verification runs and costs CPU even when it fails. Apply the same per-peer governor to `CompactBlock`, or introduce a separate, lower quota (e.g., 5–10 per second per peer) that still accommodates legitimate block relay while bounding the attack surface. Optionally, move the `compact_target` validity check (currently at line 18 of `pow/src/eaglesong.rs`, after `eaglesong()`) to before the hash computation to fail-fast on obviously invalid headers.

## Proof of Concept

```
1. Connect to target node via TCP on the CKB relay port.
2. Query current tip hash H and height N (via P2P or RPC).
3. For i in 0..∞:
     craft CompactBlock with:
       header.parent_hash    = H
       header.number         = N + 1
       header.nonce          = i          # unique hash per message
       header.compact_target = <any valid non-zero, non-overflow target>
       uncles                = []
       proposals             = []
     send message in a burst before ban takes effect;
     reconnect from new IP after ban.
4. Observe: target node CPU climbs due to repeated eaglesong() calls;
   block processing latency increases for legitimate peers.
   Each message triggers eaglesong() + 2 DB reads with zero throttle.
```

### Citations

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

**File:** sync/src/relayer/mod.rs (L195-204)
```rust
        if let Some(ban_time) = status.should_ban() {
            error_target!(
                crate::LOG_TARGET_RELAY,
                "receive {} from {}, ban {:?} for {}",
                item_name,
                peer,
                ban_time,
                status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
```

**File:** pow/src/eaglesong.rs (L11-26)
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
```

**File:** verification/src/header_verifier.rs (L32-34)
```rust
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
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

**File:** sync/src/relayer/compact_block_process.rs (L240-261)
```rust
    let status = active_chain.get_block_status(&block_hash);
    if status.contains(BlockStatus::BLOCK_STORED) {
        // update last common header and best known
        let parent = shared
            .get_header_index_view(&compact_block_header.data().raw().parent_hash(), true)
            .expect("parent block must exist");

        let header_index = HeaderIndex::new(
            compact_block_header.number(),
            block_hash.clone(),
            parent.total_difficulty() + compact_block_header.difficulty(),
        );
        let state = shared.state().peers();
        state.may_set_best_known_header(peer, header_index);

        return StatusCode::CompactBlockAlreadyStored.with_context(block_hash);
    } else if status.contains(BlockStatus::BLOCK_RECEIVED) {
        // block already in orphan pool
        return Status::ignored();
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L333-338)
```rust
            shared
                .shared()
                .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
            return StatusCode::CompactBlockHasInvalidHeader
                .with_context(format!("{block_hash} {err}"));
        }
```
