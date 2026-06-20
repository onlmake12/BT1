The code is fully readable. Let me trace the exact logic.

**`ADDR_COUNT_LIMIT = 16384`** and **`16384 = 4096 × 4`** — this is the critical arithmetic.

In `check_purge` (lines 327–404 of `peer_store_impl.rs`):

1. **Trigger condition** (line 328): fires when `count >= 16384`
2. **Step 1** (lines 341–355): collects non-connectable peers. Freshly-added addresses have `last_connected_at_ms=0`, `attempts_count=0`, `last_tried_at_ms=0` — all pass `is_connectable()` → `candidate_peers` is empty, no eviction.
3. **Step 2** (lines 358–393): groups by `/16`, takes top `len/2` groups, but only evicts from groups where `addrs.len() > 4` (line 378). If all 4096 groups have exactly 4 peers, the condition is never satisfied → `candidate_peers` is empty again.
4. **Line 400**: returns `Err(PeerStoreError::EvictionFailed)`, which propagates out of `add_addr`.

The scenario is arithmetically exact and the code path is concrete.

---

### Title
Peer store permanently blocks new address insertion when filled with exactly 4 peers per /16 group — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
`check_purge` contains a group-based eviction path that only removes peers from groups with **strictly more than 4** members. Because `ADDR_COUNT_LIMIT` equals exactly `4096 × 4 = 16384`, an attacker can fill the store with 4096 `/16` groups of exactly 4 connectable peers each, causing every subsequent `add_addr` call to return `EvictionFailed` and permanently preventing the node from learning new peer addresses.

### Finding Description

`check_purge` is called at the top of `add_addr` before inserting a new address: [1](#0-0) 

The limit constant is: [2](#0-1) 

The two-stage eviction logic:

**Stage 1** — evict non-connectable peers: [3](#0-2) 

A freshly discovered address has `last_connected_at_ms=0`, `attempts_count=0`, `last_tried_at_ms=0`. `is_connectable` returns `true` for all of them: [4](#0-3) 

So Stage 1 evicts nothing.

**Stage 2** — group-based eviction, only fires for groups with `> 4` members: [5](#0-4) 

If all 4096 groups have exactly 4 peers, `addrs.len() > 4` is false for every group, `flat_map` yields nothing, and: [6](#0-5) 

### Impact Explanation
Once the store is in this state, every call to `add_addr` returns `Err(EvictionFailed)`. The node cannot store any new peer addresses received via discovery. Peer discovery is permanently blocked until the node restarts or existing entries age out (7 days via `ADDR_TIMEOUT_MS`). This is a targeted, persistent denial-of-service against peer discovery.

### Likelihood Explanation
The P2P discovery protocol allows any connected peer to advertise arbitrary addresses. An attacker needs to:
1. Advertise addresses from 4096 distinct `/16` IPv4 groups (only 4096 of the 65536 possible `/16` blocks are needed), 4 addresses per group.
2. This is 16384 address advertisements — achievable over time through normal discovery message flow with no special privileges.

The attacker does not need to own those IPs; they only need to advertise them as peer addresses. The victim node stores them without verifying reachability at insertion time.

### Recommendation
Change the eviction threshold from `> 4` to `>= 4` (i.e., `addrs.len() >= 4`), or alternatively ensure the `take(len / 2)` pass always produces at least one candidate when the store is at capacity. A simpler fix is to also consider groups of size exactly 4 as eviction candidates when no other option exists:

```rust
// Instead of:
if addrs.len() > 4 {
// Use:
if addrs.len() >= 4 {
```

Additionally, consider adding a final fallback that evicts a random peer if both stages fail, to guarantee `add_addr` never returns `EvictionFailed` when the store is full.

### Proof of Concept
```rust
// Construct PeerStore with 4096 groups × 4 connectable peers = 16384 entries
let mut peer_store = PeerStore::default();
for group in 0u16..4096 {
    let hi = (group >> 8) as u8;
    let lo = (group & 0xff) as u8;
    for host in 1u8..=4 {
        let addr: Multiaddr = format!("/ip4/{}.{}.0.{}/tcp/8115", hi, lo, host)
            .parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
// Store is now at ADDR_COUNT_LIMIT with all connectable peers, 4 per /16 group
// Adding one more address from a new /16 group triggers EvictionFailed
let new_addr: Multiaddr = "/ip4/200.0.0.1/tcp/8115".parse().unwrap();
assert!(matches!(
    peer_store.add_addr(new_addr, Flags::COMPATIBILITY),
    Err(e) if e.to_string().contains("EvictionFailed")
));
```

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L71-79)
```rust
    pub fn add_addr(&mut self, addr: Multiaddr, flags: Flags) -> Result<()> {
        if self.ban_list.is_addr_banned(&addr) {
            return Ok(());
        }
        self.check_purge()?;
        let score = self.score_config.default_score;
        self.addr_manager
            .add(AddrInfo::new(addr, 0, score, flags.bits()));
        Ok(())
```

**File:** network/src/peer_store/peer_store_impl.rs (L341-355)
```rust
        let candidate_peers: Vec<_> = self
            .addr_manager
            .addrs_iter()
            .filter_map(|addr| {
                if !addr.is_connectable(now_ms) {
                    Some(addr.addr.clone())
                } else {
                    None
                }
            })
            .collect();

        for key in candidate_peers.iter() {
            self.addr_manager.remove(key);
        }
```

**File:** network/src/peer_store/peer_store_impl.rs (L375-392)
```rust
                    .into_iter()
                    .take(len / 2)
                    .flat_map(move |addrs| {
                        if addrs.len() > 4 {
                            Some(
                                addrs
                                    .iter()
                                    .choose_multiple(&mut rand::thread_rng(), 2)
                                    .into_iter()
                                    .map(|addr| addr.addr.clone())
                                    .collect::<Vec<Multiaddr>>(),
                            )
                        } else {
                            None
                        }
                    })
                    .flatten()
                    .collect()
```

**File:** network/src/peer_store/peer_store_impl.rs (L399-401)
```rust
            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
            }
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/types.rs (L89-105)
```rust
    pub fn is_connectable(&self, now_ms: u64) -> bool {
        // do not remove addr tried in last minute
        if self.tried_in_last_minute(now_ms) {
            return true;
        }
        // we give up if never connect to this addr
        if self.last_connected_at_ms == 0 && self.attempts_count >= ADDR_MAX_RETRIES {
            return false;
        }
        // consider addr is not connectable if failed too many times
        if now_ms.saturating_sub(self.last_connected_at_ms) > ADDR_TIMEOUT_MS
            && (self.attempts_count >= ADDR_MAX_FAILURES)
        {
            return false;
        }
        true
    }
```
