Audit Report

## Title
Peer Store Permanent DoS via Off-by-One in `check_purge` Network-Group Eviction Guard — (`network/src/peer_store/peer_store_impl.rs`)

## Summary

`check_purge` uses a strict `> 4` threshold when selecting eviction candidates from network groups. An attacker who fills the peer store with exactly 4 addresses per `/16` group across 4096 distinct groups (totalling `ADDR_COUNT_LIMIT = 16384`) causes every subsequent `add_addr` call to return `PeerStoreError::EvictionFailed`, permanently blocking honest peer address admission and enabling eclipse attacks.

## Finding Description

`check_purge` is invoked by `add_addr` before inserting a new address: [1](#0-0) 

It triggers when `addr_manager.count() >= ADDR_COUNT_LIMIT` (16384): [2](#0-1) [3](#0-2) 

**Stage 1** collects non-connectable peers. Freshly added addresses have `last_connected_at_ms = 0` and `attempts_count = 0`. `is_connectable` only returns `false` for never-connected peers when `attempts_count >= ADDR_MAX_RETRIES (3)`, so all attacker-supplied fresh addresses pass as connectable and Stage 1 finds nothing: [4](#0-3) 

**Stage 2** groups addresses by network segment, sorts by descending group size, takes the top `len/2` groups, and applies the critical filter: [5](#0-4) 

The strict `> 4` at line 378 means groups of **exactly 4** are never selected for eviction. With 4096 groups × 4 addresses = 16384 total, all groups have `len == 4`, the `flat_map` produces nothing, `candidate_peers` is empty, and the function returns: [6](#0-5) 

## Impact Explanation

After saturation, every `add_addr` call fails with `EvictionFailed`. The node can no longer learn new peer addresses from the discovery protocol, DNS seeding, or identify handshakes. The victim node is permanently stuck with only attacker-supplied addresses, enabling a full eclipse attack. This maps to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node** — the node's peer discovery is permanently disabled, effectively isolating it from the honest network.

## Likelihood Explanation

The attacker needs 16384 routable IPv4 addresses spread across 4096 distinct `/16` subnets (4 per subnet). This is achievable with a mid-sized botnet or a cloud provider with diverse IP allocations. The discovery protocol accepts `Nodes` messages from any connected peer. A single malicious peer can send crafted `Nodes` responses to fill the store. The attack is persistent: once the store is saturated in this configuration, it cannot self-heal without a node restart or manual intervention.

## Recommendation

Change the eviction threshold from strict `> 4` to `>= 4` (equivalently `> 3`) at line 378:

```rust
// Before (vulnerable):
if addrs.len() > 4 {

// After (fixed):
if addrs.len() >= 4 {
```

Additionally consider:
1. Capping the number of addresses accepted per discovery session to limit the rate at which an attacker can fill the store.
2. Preferring eviction of addresses with `last_connected_at_ms == 0` (never successfully connected) in Stage 1 before falling back to network-group eviction.

## Proof of Concept

```rust
#[test]
fn test_eviction_failed_with_exactly_4_per_group() {
    let mut peer_store = PeerStore::default();
    // Fill 4096 /16 groups × 4 addresses each = 16384 total
    for group in 0u16..4096 {
        let hi = (group >> 8) as u8;
        let lo = (group & 0xff) as u8;
        for host in 1u8..=4 {
            let addr: Multiaddr = format!("/ip4/{}.{}.1.{}/tcp/8115", hi, lo, host)
                .parse().unwrap();
            let _ = peer_store.add_addr(addr, Flags::COMPATIBILITY);
        }
    }
    // Any subsequent add_addr must fail with EvictionFailed
    let new_addr: Multiaddr = "/ip4/200.200.1.5/tcp/8115".parse().unwrap();
    assert!(matches!(
        peer_store.add_addr(new_addr, Flags::COMPATIBILITY),
        Err(e) if e.to_string().contains("EvictionFailed")
    ));
}
``` [7](#0-6)

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L71-80)
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
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L327-330)
```rust
    fn check_purge(&mut self) -> Result<()> {
        if self.addr_manager.count() < ADDR_COUNT_LIMIT {
            return Ok(());
        }
```

**File:** network/src/peer_store/peer_store_impl.rs (L374-401)
```rust
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
