### Title
Unbounded DB Read Amplification via `difficulty_boundary` Bypass of `GET_LAST_STATE_PROOF_LIMIT` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The guard at line 201 uses the client-supplied `last_n_blocks` as a proxy for total server work, but the actual number of blocks processed is `last_block_number - difficulty_boundary_block_number`. An unprivileged peer can set `difficulty_boundary` to the total difficulty of block 1 and `last_n_blocks=1`, causing the server to iterate over N−1 blocks (where N is the chain tip), each triggering O(log N) MMR DB reads — O(N log N) total — per single P2P message.

---

### Finding Description

**Entry point:** The light-client P2P handler for `GetLastStateProof` messages, reachable by any unprivileged peer.

**The guard (line 201):**

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // = 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

With `difficulties=[]` and `last_n_blocks=1`, this evaluates to `0 + 2 = 2`, which is not `> 1000`. The check passes. [1](#0-0) 

**The `else` branch (lines 298–348)** is entered when `last_block_number - start_block_number > last_n_blocks`, i.e., when N > 1. With `difficulty_boundary = total_difficulty[1]`, `get_first_block_total_difficulty_is_not_less_than` returns block 1, so `difficulty_boundary_block_number = 1`. [2](#0-1) 

**The adjustment at line 313** only increases `difficulty_boundary_block_number` when `last_block_number - difficulty_boundary_block_number < last_n_blocks`. With `difficulty_boundary_block_number=1`, `last_n_blocks=1`, and N >> 1: `N - 1 >= 1`, so the adjustment is **skipped**. [3](#0-2) 

**`last_n_numbers` is then set to `(1..N)`**, producing N−1 entries — completely unconstrained by the 1000-entry limit: [4](#0-3) 

**`complete_headers` iterates over all N−1 entries.** For each block it calls `snapshot.chain_root_mmr(number - 1).get_root()`, which performs O(log N) DB reads: [5](#0-4) 

Total DB reads per request: **O(N log N)**.

---

### Impact Explanation

A single malformed `GetLastStateProof` message causes the server to perform O(N log N) synchronous DB reads (MMR root computations + ancestor lookups) before returning. On mainnet with millions of blocks, this saturates I/O and CPU on the serving node. Because the handler is synchronous and the work is unbounded, a low-rate stream of such messages (even one per second) can sustain a denial-of-service against any light-client-serving full node.

---

### Likelihood Explanation

- No authentication or PoW is required to send `GetLastStateProof`.
- The total difficulty of block 1 is public chain data.
- The attacker only needs to know the tip hash (also public) and set `last_n_blocks=1`.
- The exploit is deterministic and reproducible on any chain with more than 2 blocks.

---

### Recommendation

Replace the limit check so it bounds the **computed** `last_n_numbers` length, not the client-supplied `last_n_blocks`. After computing `difficulty_boundary_block_number`, enforce:

```rust
if last_block_number - difficulty_boundary_block_number > GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many last_n blocks");
}
```

Alternatively, cap `difficulty_boundary_block_number` from below:

```rust
let min_boundary = last_block_number.saturating_sub(GET_LAST_STATE_PROOF_LIMIT as u64);
if difficulty_boundary_block_number < min_boundary {
    difficulty_boundary_block_number = min_boundary;
}
```

---

### Proof of Concept

```
Chain: N = 500_000 blocks on mainnet/testnet.

Attacker sends GetLastStateProof {
    last_hash:           <tip block hash>,
    start_hash:          <genesis hash>,
    start_number:        0,
    difficulty_boundary: total_difficulty[block 1],   // public
    last_n_blocks:       1,                           // passes limit check: 0 + 2 = 2 ≤ 1000
    difficulties:        [],
}

Server path:
  line 201: 0 + 2 = 2 ≤ 1000  → passes
  line 299: difficulty_boundary_block_number = 1
  line 313: 499_999 - 1 = 499_998 ≥ 1  → adjustment skipped
  line 318: last_n_numbers = (1..500_000)  → 499_999 entries
  complete_headers: 499_999 × chain_root_mmr(n-1).get_root()  → ~9.5M DB reads

Differential test:
  difficulty_boundary = total_difficulty[1]       → response time T1 (seconds–minutes)
  difficulty_boundary = total_difficulty[N-1]     → response time T2 (milliseconds)
  assert T1 >> T2  (both with last_n_blocks=1)
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L153-163)
```rust
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L299-311)
```rust
            let mut difficulty_boundary_block_number = if let Some((num, _)) = sampler
                .get_first_block_total_difficulty_is_not_less_than(
                    start_block_number,
                    last_block_number,
                    &difficulty_boundary,
                ) {
                num
            } else {
                let errmsg = format!(
                    "the difficulty boundary ({difficulty_boundary:#x}) is not in the block range [{start_block_number}, {last_block_number})"
                );
                return StatusCode::InvaildDifficultyBoundary.with_context(errmsg);
            };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L313-316)
```rust
            if last_block_number - difficulty_boundary_block_number < last_n_blocks {
                // There is not enough blocks after the difficulty boundary, so we take more.
                difficulty_boundary_block_number = last_block_number - last_n_blocks;
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L318-319)
```rust
            let last_n_numbers =
                (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
```
