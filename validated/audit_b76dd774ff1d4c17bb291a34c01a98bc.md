## Analysis

Let me trace through the exact code path to verify the claim.

### Step 1: `add_addr` entry point [1](#0-0) 

`add_addr` calls `check_purge()` before inserting. If `check_purge` returns `Err`, the new address is never added.

### Step 2: `check_purge` — Phase 1 (non-connectable eviction) [2](#0-1) 

Freshly discovered addresses are created with `last_connected_at_ms = 0` and `attempts_count = 0`. [3](#0-2) 

With `last_connected_at_ms = 0` and `attempts_count = 0`:
- `tried_in_last_minute` → false
- `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES(3)` → `0 >= 3` is **false**
- Returns `true` (connectable)

So all 16384 freshly-added addresses are connectable. Phase 1 finds zero candidates.

### Step 3: `check_purge` — Phase 2 (group-based eviction) [4](#0-3) 

With 16384 addresses each in a distinct `/16` group:
- `peers_by_network_group` has 16384 entries, each with exactly 1 address
- `len = 16384`
- `take(len / 2)` = `take(8192)` — takes 8192 groups
- For each group: `addrs.len() > 4` → `1 > 4` is **false** → returns `None`
- `candidate_peers` is empty
- Returns `Err(PeerStoreError::EvictionFailed)`

### Step 4: Group assignment confirms the attack vector [5](#0-4) 

IPv4 addresses are grouped by `[octet[0], octet[1]]` (the `/16`). Addresses `i.0.0.1` for `i in 0..256` give 256 distinct groups; using `a.b.0.1` for all 256×256 combinations gives 65536 possible groups — far more than the 16384 needed.

### Step 5: Reachable P2P entry point [6](#0-5) 

The discovery protocol is an unauthenticated P2P protocol. Any peer can send `Nodes` messages containing arbitrary addresses. The discovery handler calls `add_addr` for each received address. There is no per-session cap on how many addresses a peer can contribute to the store.

### Step 6: `ADDR_COUNT_LIMIT` confirms the threshold [7](#0-6) 

The limit is exactly 16384.

---

### Title
Peer store permanently blocks `add_addr` via `check_purge` `EvictionFailed` when filled with one address per `/16` group — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
`check_purge`'s group-based fallback only evicts from groups with **>4** peers. An attacker who fills the peer store with exactly one connectable address per `/16` group causes the fallback to produce zero eviction candidates, returning `PeerStoreError::EvictionFailed` and permanently blocking all subsequent `add_addr` calls.

### Finding Description
`check_purge` has two eviction phases:

1. **Phase 1:** Remove non-connectable addresses. Freshly discovered addresses (`last_connected_at_ms=0`, `attempts_count=0`) are always connectable, so this phase removes nothing.
2. **Phase 2:** Group addresses by `/16`, sort groups by descending size, take the top `len/2` groups, and evict 2 random peers **only from groups with >4 peers**. When every group has exactly 1 peer, no group qualifies, `candidate_peers` is empty, and `Err(PeerStoreError::EvictionFailed)` is returned.

The root cause is the hard `> 4` threshold with no fallback for the case where all groups are small.

### Impact Explanation
Once the peer store reaches 16384 addresses spread across 16384 distinct `/16` groups, every subsequent call to `add_addr` returns `Err(EvictionFailed)`. The node can no longer learn new peer addresses from the discovery protocol, effectively isolating it from peer discovery. Existing connections are unaffected, but the node cannot replace lost peers or expand its peer set.

### Likelihood Explanation
A single malicious peer connected via the discovery protocol can send crafted `Nodes` messages containing addresses from 16384 distinct `/16` groups (e.g., `a.b.0.1` for all `a,b` combinations). With `MAX_ADDR_TO_SEND = 1000` per message, roughly 17 messages suffice. No authentication, PoW, or privileged access is required — only a standard P2P connection.

### Recommendation
Replace the hard `> 4` threshold with a fallback that always evicts at least one address when the store is full. For example:
- If no group has >4 peers, evict the oldest/lowest-score address unconditionally.
- Or: evict from the largest group regardless of its size (remove the `> 4` guard entirely, replacing it with "evict from the top-N largest groups").

### Proof of Concept
```rust
let mut peer_store = PeerStore::default();
for a in 0u8..=255 {
    for b in 0u8..=63 { // 256*64 = 16384 distinct /16 groups
        let addr: Multiaddr = format!("/ip4/{}.{}.0.1/tcp/8115", a, b).parse().unwrap();
        // First 16383 succeed; store reaches ADDR_COUNT_LIMIT
        let _ = peer_store.add_addr(addr, Flags::COMPATIBILITY);
    }
}
// 16385th address from a new /16 group
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

**File:** network/src/peer_store/peer_store_impl.rs (L358-401)
```rust
            let candidate_peers: Vec<_> = {
                let mut peers_by_network_group: HashMap<Group, Vec<_>> = HashMap::default();
                for addr in self.addr_manager.addrs_iter() {
                    peers_by_network_group
                        .entry((&addr.addr).into())
                        .or_default()
                        .push(addr);
                }
                let len = peers_by_network_group.len();
                let mut peers = peers_by_network_group
                    .drain()
                    .map(|(_, v)| v)
                    .collect::<Vec<Vec<_>>>();

                peers.sort_unstable_by_key(|k| std::cmp::Reverse(k.len()));

                peers
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
            };

            for key in candidate_peers.iter() {
                self.addr_manager.remove(key);
            }

            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
            }
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

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
```

**File:** network/src/protocols/discovery/mod.rs (L1-36)
```rust
use std::{collections::HashMap, sync::Arc};

use ckb_logger::{debug, error, trace, warn};
use ckb_systemtime::{Duration, Instant};
use p2p::{
    SessionId, async_trait, bytes,
    context::{ProtocolContext, ProtocolContextMutRef, SessionContext},
    multiaddr::Multiaddr,
    traits::ServiceProtocol,
    utils::{is_reachable, multiaddr_to_socketaddr},
};
use rand::seq::SliceRandom;

pub use self::{
    addr::{AddressManager, MisbehaveResult, Misbehavior},
    protocol::{DiscoveryMessage, Node, Nodes},
    state::SessionState,
};
use self::{
    protocol::{decode, encode},
    state::RemoteAddress,
};
use crate::{Flags, NetworkState, ProtocolId};

mod addr;
pub(crate) mod protocol;
mod state;

const ANNOUNCE_CHECK_INTERVAL: Duration = Duration::from_secs(60);
const ANNOUNCE_THRESHOLD: usize = 10;
// The maximum number of new addresses to accumulate before announcing.
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
// Every 24 hours send announce nodes message
const ANNOUNCE_INTERVAL: Duration = Duration::from_secs(3600 * 24);
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
