Audit Report

## Title
Unbounded `last_n_numbers` Vector via Minimum `difficulty_boundary` Bypasses `GET_LAST_STATE_PROOF_LIMIT` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary

`GetLastStateProofProcess::execute` enforces a size limit using `last_n_blocks` as a proxy for server work, but the actual work is determined by `difficulty_boundary_block_number`, which is resolved from the attacker-controlled `difficulty_boundary` field. Setting `difficulty_boundary=1` causes the binary search to resolve to block 0 immediately, making `last_n_numbers` span `(0..last_block_number)` — potentially millions of entries — completely bypassing the `GET_LAST_STATE_PROOF_LIMIT=1000` guard. Each entry then triggers multiple O(log N) RocksDB reads and an MMR root computation in `complete_headers`, enabling a single-packet DoS against any light-client-enabled CKB node.

## Finding Description

**Step 1 — Limit check passes:**

The guard at lines 201–205 only bounds `last_n_blocks`, not the actual range that will be iterated:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

With `difficulties=[]` and `last_n_blocks=499`: `0 + 499×2 = 998 ≤ 1000` → passes. [1](#0-0) [2](#0-1) 

**Step 2 — Branch selection enters `else`:**

With `start_block_number=0`, `last_block_number=10_000_000`, `last_n_blocks=499`: `10_000_000 - 0 <= 499` is false, so execution enters the `else` branch where `difficulty_boundary` is resolved. [3](#0-2) 

**Step 3 — Binary search short-circuits at block 0:**

```rust
if let Some(start_total_difficulty) = self.get_block_total_difficulty(start_block_number) {
    if start_total_difficulty >= *min_total_difficulty {
        return Some((start_block_number, start_total_difficulty));
    }
}
```

With `start_block_number=0` and `min_total_difficulty=1`, the genesis block's total difficulty is always ≥ 1, so `get_first_block_total_difficulty_is_not_less_than` returns `(0, genesis_difficulty)` after exactly 2 RocksDB reads. `difficulty_boundary_block_number` is set to 0. [4](#0-3) 

**Step 4 — Adjustment guard does not fire:**

```rust
if last_block_number - difficulty_boundary_block_number < last_n_blocks {
    difficulty_boundary_block_number = last_block_number - last_n_blocks;
}
```

With `difficulty_boundary_block_number=0` and `last_block_number=10_000_000`: `10_000_000 - 0 < 499` is false. The guard is designed to handle the case where the boundary is *too close to the tip*, not the case where it is *too far from the tip*. `difficulty_boundary_block_number` remains 0. [5](#0-4) 

**Step 5 — Unbounded `last_n_numbers` allocation:**

```rust
let last_n_numbers =
    (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
```

This allocates a `Vec<u64>` of `last_block_number` entries (~10M on mainnet), not `last_n_blocks=499`. [6](#0-5) 

**Step 6 — `complete_headers` iterates all entries:**

For each of the millions of block numbers, the server performs:
- `snapshot.get_ancestor(last_hash, *number)` — O(log N) RocksDB reads
- `snapshot.get_block(&ancestor_header.hash())` — additional RocksDB reads
- `snapshot.chain_root_mmr(*number - 1).get_root()` — O(log N) MMR computation [7](#0-6) 

**Step 7 — No rate limiting:**

The `received` handler processes each message synchronously with no per-peer throttle, no concurrent request cap, and no cooldown. A peer is only banned for sending a *malformed* message, not for sending a valid but expensive one. [8](#0-7) 

**Additional confirmation — `start_block_number=0` bypasses all input validation:**

When `start_block_number=0`, the reorg check at lines 237–247 immediately returns `Vec::new()` without verifying `start_hash`, and the difficulty-vs-start-block check at lines 268–288 is skipped because `difficulties=[]`. [9](#0-8) 

## Impact Explanation

A single crafted `GetLastStateProof` message (~200 bytes) forces the server to allocate a vector of ~10M entries and perform O(N × log N) RocksDB reads and MMR computations where N = chain height. This saturates the node's I/O and CPU, preventing it from processing new blocks, serving legitimate peers, or maintaining sync. The unbounded `Vec` allocation also risks OOM. Repeated at any rate (even once per connection), this constitutes a sustained DoS. This matches **High (10001–15000 points)**: *"Vulnerabilities which could easily crash a CKB node"* and *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation

The attack requires only a valid P2P connection to a light-client-enabled CKB node. No PoW, no stake, no key, no privilege is required. The crafted message is trivially constructable from public chain data (`last_hash` obtained via `GetLastState`; `start_hash` is irrelevant when `start_block_number=0`). The exploit path is fully deterministic, locally testable, and repeatable with new connections after any ban.

## Recommendation

After computing `difficulty_boundary_block_number`, enforce that the resolved range does not exceed `last_n_blocks` before collecting `last_n_numbers`:

```rust
if last_block_number - difficulty_boundary_block_number > last_n_blocks {
    return StatusCode::InvalidRequest
        .with_context("difficulty_boundary resolves too early");
}
```

This check must be inserted *before* the adjustment guard and *before* `last_n_numbers` is collected at lines 318–319. Alternatively, validate that `difficulty_boundary` corresponds to a block number no earlier than `last_block_number - last_n_blocks` before performing any further computation. [10](#0-9) 

## Proof of Concept

```
GetLastStateProof {
    last_hash:           <current tip hash>,   // from GetLastState
    start_hash:          <any bytes>,          // irrelevant when start_number=0
    start_number:        0,
    last_n_blocks:       499,
    difficulty_boundary: U256::from(1u64),
    difficulties:        [],
}
```

1. Limit check: `0 + 499×2 = 998 ≤ 1000` → passes.
2. `last_block_number - 0 > 499` → enters `else` branch.
3. Binary search: genesis total difficulty ≥ 1 → returns `(0, genesis_difficulty)` after 2 reads.
4. Adjustment guard: `last_block_number - 0 < 499` → false → no adjustment.
5. `last_n_numbers = (0..last_block_number)` → ~10M entries allocated.
6. `complete_headers` performs ~10M × O(log N) RocksDB + MMR operations.
7. Node I/O and memory saturated; repeat from step 1 with a new connection.

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L30-33)
```rust
        if let Some(start_total_difficulty) = self.get_block_total_difficulty(start_block_number) {
            if start_total_difficulty >= *min_total_difficulty {
                return Some((start_block_number, start_total_difficulty));
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-163)
```rust
        for number in numbers {
            if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
                let position = leaf_index_to_pos(*number);
                positions.push(position);

                let ancestor_block = self
                    .snapshot
                    .get_block(&ancestor_header.hash())
                    .ok_or_else(|| {
                        format!(
                            "failed to find block for header#{} (hash: {:#x})",
                            number,
                            ancestor_header.hash()
                        )
                    })?;
                let uncles_hash = ancestor_block.calc_uncles_hash();
                let extension = ancestor_block.extension();

                let parent_chain_root = if *number == 0 {
                    Default::default()
                } else {
                    let mmr = self.snapshot.chain_root_mmr(*number - 1);
                    match mmr.get_root() {
                        Ok(root) => root,
                        Err(err) => {
                            let errmsg = format!(
                                "failed to generate a root for block#{number} since {err:?}"
                            );
                            return Err(errmsg);
                        }
                    }
                };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L237-247)
```rust
        let reorg_last_n_numbers = if start_block_number == 0
            || snapshot
                .get_ancestor(&last_block_hash, start_block_number)
                .map(|header| header.hash() == start_block_hash)
                .unwrap_or(false)
        {
            Vec::new()
        } else {
            let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
            (min_block_number..start_block_number).collect()
        };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L291-298)
```rust
        let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
            <= last_n_blocks
        {
            // There is not enough blocks, so we take all of them; so there is no sampled blocks.
            let sampled_numbers = Vec::new();
            let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
            (sampled_numbers, last_n_numbers)
        } else {
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L313-319)
```rust
            if last_block_number - difficulty_boundary_block_number < last_n_blocks {
                // There is not enough blocks after the difficulty boundary, so we take more.
                difficulty_boundary_block_number = last_block_number - last_n_blocks;
            }

            let last_n_numbers =
                (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/lib.rs (L55-92)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        data: Bytes,
    ) {
        trace!("LightClient.received peer={}", peer);

        let msg = match packed::LightClientMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "LightClient.received a malformed message from Peer({})",
                    peer
                );
                nc.ban_peer(
                    peer,
                    constant::BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let item_name = msg.item_name();
        let status = self.try_process(&nc, peer, msg).await;
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, peer, ban_time, status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
        } else if status.should_warn() {
            warn!("process {} from {}; result is {}", item_name, peer, status);
        } else if !status.is_ok() {
            debug!("process {} from {}; result is {}", item_name, peer, status);
        }
    }
```
