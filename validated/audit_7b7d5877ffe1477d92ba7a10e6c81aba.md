The code matches all cited line numbers and logic exactly. Let me verify the exploit path step by step against the actual source.

All cited line numbers, logic, and code references are confirmed against the actual source. The exploit path traces cleanly through the real code with no gaps.

Audit Report

## Title
Unbounded `last_n_numbers` Vec Allocation via Attacker-Controlled `difficulty_boundary` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary

The `GET_LAST_STATE_PROOF_LIMIT` guard bounds only `difficulties.len() + last_n_blocks * 2`, not the actual size of `last_n_numbers` allocated later. By sending `difficulty_boundary = U256::one()` with `last_n_blocks = 1` and `difficulties = []`, a remote peer causes `difficulty_boundary_block_number` to resolve to 0 (genesis), bypasses the sole adjustment guard, and triggers `(0..N).collect()` followed by N iterations of `complete_headers` — where N is the full chain height — causing OOM or sustained CPU exhaustion.

## Finding Description

**Guard (lines 201–205):** `difficulties.len() + last_n_blocks * 2 = 0 + 2 = 2`, which does not exceed `GET_LAST_STATE_PROOF_LIMIT = 1000`. The guard passes. [1](#0-0) [2](#0-1) 

**Branch selection (lines 291–292):** With `start_block_number = 0`, `last_n_blocks = 1`, and chain height N > 1, the condition `N - 0 <= 1` is false, so execution enters the `else` branch. [3](#0-2) 

**Early return in `get_first_block_total_difficulty_is_not_less_than` (lines 30–33):** With `start_block_number = 0` and `min_total_difficulty = U256::one()`, the genesis block's total difficulty is always ≥ 1, so the function immediately returns `Some((0, genesis_td))`, setting `difficulty_boundary_block_number = 0`. [4](#0-3) 

**Adjustment guard (lines 313–316):** The only corrective guard checks `N - 0 < 1`, which is false for any chain height > 1. No correction is applied. [5](#0-4) 

**Unbounded allocation (lines 318–319):** `(0..N).collect::<Vec<_>>()` allocates N `u64` elements — the entire chain height — with no bound. [6](#0-5) 

**`complete_headers` loop (lines 132–177):** For each of the N block numbers, the function calls `snapshot.get_ancestor` (O(log N) skip-list traversal), `snapshot.get_block`, `calc_uncles_hash`, and `snapshot.chain_root_mmr(*number - 1).get_root()` (MMR root computation). Total work is O(N log N), blocking the async handler. [7](#0-6) 

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.** For a chain at height N = 10,000,000, the `last_n_numbers` Vec alone is ~80 MB. The `Vec<packed::VerifiableHeader>` built in `complete_headers` is substantially larger (each entry includes header data, uncles hash, extension, and MMR root). The O(N log N) CPU work blocks the async handler. A single malicious peer can trigger repeated OOM crashes or sustained CPU exhaustion, rendering the light-client server permanently unresponsive.

## Likelihood Explanation

The light-client P2P protocol is open to any peer with no authentication, PoW, or stake requirement. The `GetLastStateProof` message is trivially constructable. The attacker only needs the current tip hash (publicly broadcast via `SendLastState`) and knowledge that the chain height exceeds 1 block — true on mainnet from block 2 onward. The attack is repeatable: after a node restart, the same peer (or any peer) can immediately re-trigger it.

## Recommendation

After resolving `difficulty_boundary_block_number`, unconditionally clamp it so that `last_n_numbers` cannot exceed `last_n_blocks` elements. Replace the existing one-sided guard with a single unconditional assignment:

```rust
difficulty_boundary_block_number =
    difficulty_boundary_block_number.max(last_block_number - last_n_blocks);
```

Additionally, restructure the guard at lines 201–205 to bound the **effective** `last_n_numbers` size (i.e., `last_n_blocks` capped at chain height), not just the raw `last_n_blocks` field from the message.

## Proof of Concept

Send the following `GetLastStateProof` message to any light-client server node synced past block 1:

```
GetLastStateProof {
    last_hash:           <current tip hash from SendLastState>,
    start_hash:          <genesis block hash>,
    start_number:        0,
    last_n_blocks:       1,
    difficulty_boundary: 0x0000...0001,  // U256::one()
    difficulties:        [],
}
```

Execution trace:
1. Guard: `0 + 1*2 = 2 ≤ 1000` → **passes**
2. `N - 0 <= 1` → **false** → enters `else` branch
3. `get_first_block_total_difficulty_is_not_less_than(0, N, 1)` → genesis_td ≥ 1 → returns `Some((0, genesis_td))`
4. `difficulty_boundary_block_number = 0`
5. `N - 0 < 1` → **false** → no adjustment
6. `(0..N).collect::<Vec<_>>()` → **N-element Vec allocated**
7. `complete_headers` loops N times → **OOM / CPU exhaustion**

A unit test can be written using the existing `MockChain` harness (as used in `util/light-client-protocol-server/src/tests/components/get_last_state_proof.rs`) by mining to a large height and sending the above crafted message, then asserting the peer is banned and no response is sent within a bounded time. [8](#0-7)

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L30-33)
```rust
        if let Some(start_total_difficulty) = self.get_block_total_difficulty(start_block_number) {
            if start_total_difficulty >= *min_total_difficulty {
                return Some((start_block_number, start_total_difficulty));
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-177)
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

                let header = packed::VerifiableHeader::new_builder()
                    .header(ancestor_header.data())
                    .uncles_hash(uncles_hash)
                    .extension(Pack::pack(&extension))
                    .parent_chain_root(parent_chain_root)
                    .build();

                headers.push(header);
            } else {
                let errmsg = format!("failed to find ancestor header ({number})");
                return Err(errmsg);
            }
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
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

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/tests/components/get_last_state_proof.rs (L14-50)
```rust
#[tokio::test(flavor = "multi_thread")]
async fn get_last_state_proof_with_the_genesis_block() {
    let chain = MockChain::new();
    let nc = MockNetworkContext::new(SupportProtocols::LightClient);

    chain.mine_to(1);

    let snapshot = chain.shared().snapshot();
    let verifiable_tip_header: VerifiableHeader =
        snapshot.get_verifiable_header_by_number(1).unwrap().into();
    let tip_header = verifiable_tip_header.header();
    let genesis_header = snapshot.get_header_by_number(0).unwrap();

    let mut protocol = chain.create_light_client_protocol();

    let data = {
        let content = packed::GetLastStateProof::new_builder()
            .last_hash(tip_header.hash())
            .start_hash(genesis_header.hash())
            .start_number(0u64)
            .last_n_blocks(10u64)
            .difficulty_boundary(genesis_header.difficulty())
            .build();
        packed::LightClientMessage::new_builder()
            .set(content)
            .build()
    }
    .as_bytes();

    assert!(nc.sent_messages().borrow().is_empty());

    let peer_index = PeerIndex::new(1);
    protocol.received(nc.context(), peer_index, data).await;

    assert!(nc.not_banned(peer_index));

    assert_eq!(nc.sent_messages().borrow().len(), 1);
```
