Audit Report

## Title
Unbounded CPU Exhaustion via Rate-Limit Exemption for CompactBlock Messages — (`sync/src/relayer/mod.rs`, `pow/src/eaglesong.rs`)

## Summary
`CompactBlock` messages are unconditionally exempted from the per-peer rate limiter in `sync/src/relayer/mod.rs`. Because `EaglesongPowEngine::verify` always executes the full `eaglesong()` hash computation before checking the result, and because the BLOCK_INVALID cache is keyed on block hash (bypassed by varying the nonce), an attacker can force repeated PoW computations and DB reads per connection with no application-layer throttle. This constitutes a High-severity denial-of-service path capable of causing CKB network congestion with low attacker cost.

## Finding Description

**Rate-limit bypass — confirmed in source**

`sync/src/relayer/mod.rs` lines 112–114 unconditionally skip the rate limiter for every `CompactBlock` message:

```rust
// CompactBlock will be verified by POW, it's OK to skip rate limit checking.
let should_check_rate =
    !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));
```

All other relay message types are gated by a 30-req/s per-`(PeerIndex, message_type)` governor limiter; `CompactBlock` is not.

**PoW runs unconditionally before the result is checked**

`pow/src/eaglesong.rs` `EaglesongPowEngine::verify` calls `eaglesong()` on every invocation before comparing the output to the target:

```rust
fn verify(&self, header: &Header) -> bool {
    let input = crate::pow_message(&header.as_reader().calc_pow_hash(), header.nonce().into());
    let mut output = [0u8; 32];
    eaglesong(&input, &mut output);
    // ... then checks output vs block_target
```

**PoW is the first check inside `HeaderVerifier::verify`**

`verification/src/header_verifier.rs` line 34 calls `PowVerifier` before any other check (number, epoch, timestamp):

```rust
PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
```

**`non_contextual_check` is trivially satisfied**

The only pre-`contextual_check` gate checks uncle count ≤ `max_uncles_num`, proposals count ≤ `max_block_proposals_limit`, and block height ≥ `tip − epoch_length`. All three are trivially satisfied by a crafted header with 0 uncles, 0 proposals, and `height = tip + 1`.

**`contextual_check` cache is bypassed by varying the nonce**

`contextual_check` has early exits for `BLOCK_STORED`, `BLOCK_RECEIVED`, `BLOCK_INVALID`, missing parent, and already-pending-for-this-peer — all keyed on the **block hash**. An attacker who varies the nonce field produces a fresh hash on every message, bypassing every cache. After PoW fails, the hash is marked `BLOCK_INVALID` at lines 333–335, but the next message with a different nonce starts fresh.

**Peer banning is post-hoc and pipelineable**

After `try_process` returns `CompactBlockHasInvalidHeader`, `process()` at lines 195–204 calls `nc.ban_peer()`. However, banning occurs only after the message is fully processed. In an async network stack, an attacker can pipeline a burst of `CompactBlock` messages before the first ban takes effect, obtaining multiple eaglesong computations per connection. Additionally, the attacker can reconnect from different IPs after each ban, as the ban is IP-keyed.

## Impact Explanation

Per malicious message the victim node executes:
1. One `eaglesong()` hash computation (CPU-bound)
2. `active_chain.get_block_status()` — DB read
3. `shared.get_header_index_view()` — DB read for parent
4. `pending_compact_blocks().await` — async lock acquisition

With no rate limit on `CompactBlock`, a burst of pipelined messages per connection (before ban takes effect) and/or multiple connections from different IPs can sustain elevated CPU and I/O load. This matches the allowed High-severity impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** Block-processing latency increases for legitimate peers, degrading node responsiveness.

## Likelihood Explanation

The attack requires only a standard TCP connection to a synced node. The attacker needs one valid parent hash (publicly observable from the P2P network or RPC). Crafting headers with varying nonces is trivial — no PoW solution is required. The exemption is unconditional and applies to all peers regardless of reputation. While per-IP banning limits sustained single-connection attacks, the attacker can pipeline messages before the ban and rotate IPs cheaply (VPS, botnet, Tor exit nodes), making the attack repeatable at low cost.

## Recommendation

Remove the blanket rate-limit exemption for `CompactBlock`. The developer comment ("CompactBlock will be verified by POW") conflates *solving* PoW with *verifying* PoW — verification runs and costs CPU even when it fails. Apply the same per-peer governor to `CompactBlock`, or introduce a separate, lower quota (e.g., 5–10 per second per peer) that still accommodates legitimate block relay while bounding the attack surface. Optionally, move a cheap structural check (e.g., `compact_target` validity) before the full `eaglesong()` call to fail-fast on obviously invalid headers.

## Proof of Concept

```
1. Connect to target node via TCP on the CKB relay port.
2. Query current tip hash H and height N (via P2P or RPC).
3. For i in 0..∞:
     craft CompactBlock with:
       header.parent_hash   = H
       header.number        = N + 1
       header.nonce         = i          # unique hash per message
       header.compact_target = <any valid-looking target>
       uncles               = []
       proposals            = []
     send message in a burst before ban takes effect;
     reconnect from new IP after ban.
4. Observe: target node CPU climbs due to repeated eaglesong() calls;
   block processing latency increases for legitimate peers.
   Each message triggers eaglesong() + 2 DB reads with zero throttle.
```