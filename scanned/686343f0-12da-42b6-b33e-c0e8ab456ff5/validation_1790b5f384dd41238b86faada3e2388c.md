The vulnerability is real and concrete. Here is the full trace:

---

### Title
Peer Store Permanently Blocked by Onion3 Address Flood via `check_purge` `take(len/2)=0` Bug — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
When `ADDR_COUNT_LIMIT` (16 384) addresses that all map to `Group::None` fill the `AddrManager`, the second-pass eviction in `check_purge` computes `len = 1` (one bucket), calls `take(1/2) = take(0)`, produces zero candidates, and unconditionally returns `EvictionFailed`. Every subsequent `add_addr` call fails, permanently preventing the node from learning new peer addresses.

### Finding Description

**Root cause — `network_group.rs` `From<&Multiaddr> for Group`**

`multiaddr_to_socketaddr` returns `None` for any non-TCP/UDP address (Onion3, DNS, etc.), so the conversion falls through to the catch-all: [1](#0-0) 

Every Onion3 multiaddr therefore hashes to the same `Group::None` bucket.

**Trigger path — `peer_store_impl.rs` `check_purge`**

`add_addr` calls `check_purge` before inserting: [2](#0-1) 

`check_purge` first tries to evict non-connectable addresses. Fresh Onion3 entries have `last_connected_at_ms = 0` and `attempts_count = 0`; `is_connectable` returns `true` because `0 < ADDR_MAX_RETRIES (3)`: [3](#0-2) 

So the first pass evicts nothing. The second pass groups all addresses, then: [4](#0-3) 

With all 16 384 addresses in one `Group::None` bucket, `len = 1`. Integer division `1 / 2 = 0`, so `take(0)` yields an empty iterator. `candidate_peers` is empty, and the function returns: [5](#0-4) 

**`ADDR_COUNT_LIMIT` value:** [6](#0-5) 

### Impact Explanation
Once the store is saturated with Onion3 addresses, every `add_addr` call returns `EvictionFailed`. The node can no longer learn new peer addresses from the discovery protocol, including legitimate IPv4/IPv6 peers. Existing connections are unaffected, but the node's ability to find new peers is permanently disabled until it is restarted and the persisted peer store is cleared.

### Likelihood Explanation
The CKB discovery protocol allows any connected peer to advertise arbitrary multiaddrs. An attacker controlling one or more peers can stream Onion3 addresses (each with a distinct 35-byte host, so all unique) through the discovery `GetNodes`/`Nodes` messages. 16 384 addresses at typical discovery batch sizes (up to 1 000 per message) requires only ~17 messages. No PoW, no privileged role, and no Sybil majority is required — a single malicious peer is sufficient.

### Recommendation
Fix the `take(len / 2)` expression to guarantee at least one group is selected when the store is full:

```rust
// Replace:
.take(len / 2)
// With:
.take((len / 2).max(1))
```

Additionally, consider capping the number of `Group::None` addresses that can be stored (e.g., reject new `Group::None` addresses once they exceed `ADDR_COUNT_LIMIT / N` for some N), mirroring Bitcoin Core's per-bucket limits.

### Proof of Concept

```rust
#[test]
fn test_onion3_eviction_failure() {
    let mut peer_store = PeerStore::default();
    // Fill store with ADDR_COUNT_LIMIT distinct Onion3 addresses
    for i in 0..ADDR_COUNT_LIMIT {
        let onion_host = format!("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa{:05}", i); // 35-char host
        let addr: Multiaddr = format!("/onion3/{}:1234", onion_host).parse().unwrap();
        // Use add_outbound_addr to bypass check_purge during fill
        peer_store.add_outbound_addr(addr, Flags::COMPATIBILITY);
    }
    // Now attempt to add one more address — must trigger check_purge
    let new_addr: Multiaddr = "/ip4/1.2.3.4/tcp/8115".parse().unwrap();
    let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
    // Asserts EvictionFailed — new legitimate IPv4 peer is rejected
    assert!(result.is_err());
}
```

### Citations

**File:** network/src/network_group.rs (L39-42)
```rust
        }
        // Can't group addr
        Group::None
    }
```

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

**File:** network/src/peer_store/peer_store_impl.rs (L366-376)
```rust
                let len = peers_by_network_group.len();
                let mut peers = peers_by_network_group
                    .drain()
                    .map(|(_, v)| v)
                    .collect::<Vec<Vec<_>>>();

                peers.sort_unstable_by_key(|k| std::cmp::Reverse(k.len()));

                peers
                    .into_iter()
                    .take(len / 2)
```

**File:** network/src/peer_store/peer_store_impl.rs (L399-401)
```rust
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
