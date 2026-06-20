### Title
`CandidateUncles::insert` Evicts Oldest Uncle Group Before Checking Duplicate Presence — (`tx-pool/src/block_assembler/candidate_uncles.rs`)

---

### Summary

`CandidateUncles::insert` unconditionally evicts the oldest uncle group when the pool is full and the incoming uncle has a higher block number, **without first checking whether the incoming uncle is already present in the pool**. If the uncle is a duplicate, the eviction is wasted and the pool permanently shrinks below `MAX_CANDIDATE_UNCLES`, allowing any peer to drain legitimate uncle candidates from the miner's block template.

---

### Finding Description

`CandidateUncles` is a bounded pool capped at `MAX_CANDIDATE_UNCLES = 128` total entries, organized as a `BTreeMap<BlockNumber, HashSet<UncleBlockView>>` with a separate `count` field. [1](#0-0) 

The `insert` method's eviction path is:

```rust
pub fn insert(&mut self, uncle: UncleBlockView) -> bool {
    let number: BlockNumber = uncle.header().number();
    if self.count >= MAX_CANDIDATE_UNCLES {
        let first_key = *self.map.keys().next().expect("length checked");
        if number > first_key {
            if let Some(set) = self.map.remove(&first_key) {
                self.count -= set.len();   // ← eviction committed here
            }
        } else {
            return false;
        }
    }

    let set = self.map.entry(number).or_default();
    if set.len() < MAX_PER_HEIGHT {
        let ret = set.insert(uncle);       // ← returns false if already present
        if ret {
            self.count += 1;
        }
        ret
    } else {
        false
    }
}
``` [2](#0-1) 

When the pool is full and `number > first_key`:

1. The entire `HashSet` at `first_key` is removed and `self.count` is decremented by its length.
2. Only **after** the eviction does `set.insert(uncle)` run — which returns `false` (and skips the `count += 1`) if the uncle is already present.

Net result: the oldest group is permanently evicted, but the incoming uncle was never actually new. The pool now holds fewer than `MAX_CANDIDATE_UNCLES` entries.

The `contains` method exists and is correct, but is never called before the eviction: [3](#0-2) 

The entry point reachable from an unprivileged peer is `receive_candidate_uncle`, which directly calls `candidate_uncles.insert`: [4](#0-3) 

Uncle blocks are relayed over the P2P network by any connected peer. A second insertion path exists during chain reorgs (`update_block_assembler_before_tx_pool_reorg`): [5](#0-4) 

---

### Impact Explanation

- An attacker who observes an uncle already in the candidate pool (all uncle relays are public) can re-relay it whenever the pool is full and the uncle's block number exceeds the oldest group's number.
- Each such re-relay evicts the entire oldest group (up to `MAX_PER_HEIGHT = 10` uncles) while adding nothing new.
- Repeated attacks drain the pool, causing the miner's block template to contain fewer or no uncle references, directly reducing uncle rewards.
- The pool's `count` field permanently drifts below `MAX_CANDIDATE_UNCLES`, meaning the pool never refills to capacity as long as the attack continues.

---

### Likelihood Explanation

- Uncle blocks are publicly broadcast; any peer can observe which uncles are in circulation.
- The pool capacity is 128; on a busy network this fills regularly.
- The attack requires only sending a single already-known uncle message per eviction cycle — no special privilege, no key, no majority hashpower.
- The `MAX_PER_HEIGHT = 10` multiplier means one message can evict up to 10 uncles at once.

---

### Recommendation

Check for duplicate presence **before** performing the eviction:

```rust
pub fn insert(&mut self, uncle: UncleBlockView) -> bool {
    // Early-exit if already present — no eviction should occur
    if self.contains(&uncle) {
        return false;
    }

    let number: BlockNumber = uncle.header().number();
    if self.count >= MAX_CANDIDATE_UNCLES {
        let first_key = *self.map.keys().next().expect("length checked");
        if number > first_key {
            if let Some(set) = self.map.remove(&first_key) {
                self.count -= set.len();
            }
        } else {
            return false;
        }
    }

    let set = self.map.entry(number).or_default();
    if set.len() < MAX_PER_HEIGHT {
        let ret = set.insert(uncle);
        if ret {
            self.count += 1;
        }
        ret
    } else {
        false
    }
}
```

---

### Proof of Concept

1. Fill `CandidateUncles` to `MAX_CANDIDATE_UNCLES = 128` with uncles at block heights 1–128 (one per height).
2. Observe uncle `U` at height 100 is in the pool; the oldest group is at height 1.
3. Call `insert(U)` a second time (re-relay uncle `U`).
4. `self.count >= 128` → true; `first_key = 1`; `100 > 1` → true.
5. The set at height 1 is removed; `self.count` drops to 127.
6. `set.insert(U)` at height 100 returns `false` (already present); `self.count` stays at 127.
7. Pool now has 127 entries — the uncle at height 1 is permanently gone.
8. Repeat step 3 to continuously evict the next-oldest group, draining the pool. [6](#0-5) [7](#0-6)

### Citations

**File:** tx-pool/src/block_assembler/candidate_uncles.rs (L7-21)
```rust
#[cfg(not(test))]
const MAX_CANDIDATE_UNCLES: usize = 128;
#[cfg(test)]
pub(crate) const MAX_CANDIDATE_UNCLES: usize = 4;

#[cfg(not(test))]
const MAX_PER_HEIGHT: usize = 10;
#[cfg(test)]
pub(crate) const MAX_PER_HEIGHT: usize = 2;

/// Candidate uncles container
pub struct CandidateUncles {
    pub(crate) map: BTreeMap<BlockNumber, HashSet<UncleBlockView>>,
    count: usize,
}
```

**File:** tx-pool/src/block_assembler/candidate_uncles.rs (L35-58)
```rust
    pub fn insert(&mut self, uncle: UncleBlockView) -> bool {
        let number: BlockNumber = uncle.header().number();
        if self.count >= MAX_CANDIDATE_UNCLES {
            let first_key = *self.map.keys().next().expect("length checked");
            if number > first_key {
                if let Some(set) = self.map.remove(&first_key) {
                    self.count -= set.len();
                }
            } else {
                return false;
            }
        }

        let set = self.map.entry(number).or_default();
        if set.len() < MAX_PER_HEIGHT {
            let ret = set.insert(uncle);
            if ret {
                self.count += 1;
            }
            ret
        } else {
            false
        }
    }
```

**File:** tx-pool/src/block_assembler/candidate_uncles.rs (L78-84)
```rust
    pub fn contains(&self, uncle: &UncleBlockView) -> bool {
        let number: BlockNumber = uncle.header().number();
        self.map
            .get(&number)
            .map(|set| set.contains(uncle))
            .unwrap_or(false)
    }
```

**File:** tx-pool/src/service.rs (L1210-1224)
```rust
    pub async fn receive_candidate_uncle(&self, uncle: UncleBlockView) {
        if let Some(ref block_assembler) = self.block_assembler {
            {
                block_assembler.candidate_uncles.lock().await.insert(uncle);
            }
            if self
                .block_assembler_sender
                .send(BlockAssemblerMessage::Uncle)
                .await
                .is_err()
            {
                error!("block_assembler receiver dropped");
            }
        }
    }
```

**File:** tx-pool/src/service.rs (L1226-1244)
```rust
    pub async fn update_block_assembler_before_tx_pool_reorg(
        &self,
        detached_blocks: VecDeque<BlockView>,
        snapshot: Arc<Snapshot>,
    ) {
        if let Some(ref block_assembler) = self.block_assembler {
            {
                let mut candidate_uncles = block_assembler.candidate_uncles.lock().await;
                for detached_block in detached_blocks {
                    candidate_uncles.insert(detached_block.as_uncle());
                }
            }

            if let Err(e) = block_assembler.update_blank(snapshot).await {
                error!("block_assembler update_blank error {}", e);
            }
            block_assembler.notify().await;
        }
    }
```
