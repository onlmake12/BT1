Based on my analysis of the actual code, this vulnerability is **real and exploitable**.

---

### Title
Score Reset via Discovery Re-Advertisement Bypasses Peer Penalty Accumulation — (`network/src/peer_store/addr_manager.rs`, `network/src/peer_store/peer_store_impl.rs`)

### Summary

`AddrManager::add` unconditionally overwrites an existing `AddrInfo` entry — including its `score` field — when the incoming `last_connected_at_ms` is `>=` the stored value. Because `add_addr` always constructs a new `AddrInfo` with `last_connected_at_ms=0` and `score=default_score`, any peer whose stored entry also has `last_connected_at_ms=0` (i.e., discovered but never successfully connected) can have its penalized score silently reset to 100 by any remote peer re-advertising the same address through the discovery protocol.

### Finding Description

`PeerStore::add_addr` always creates a fresh `AddrInfo` with `last_connected_at_ms=0` and `score=default_score (100)`: [1](#0-0) 

This is passed to `AddrManager::add`, which checks only the timestamp to decide whether to overwrite: [2](#0-1) 

The guard on line 29 is `addr_info.last_connected_at_ms >= exist_last_connected_at_ms`. When both values are `0` (the peer was only ever discovered, never connected), `0 >= 0` is `true`, and the entire entry — including `score` — is replaced with the fresh `AddrInfo` carrying `score=100`.

`PeerStore::report` reduces score in-place: [3](#0-2) 

But it only bans when `score < ban_score (40)`. Any score reduction that leaves the peer above 40 is silently erased by a subsequent `add_addr` call for the same address.

### Impact Explanation

A misbehaving peer can never accumulate enough penalty to reach `ban_score` if it (or a colluding peer) re-advertises its address through the discovery protocol after each bad-behavior report. With `default_score=100` and `ban_score=40`, a peer that loses 20 points per infraction would normally be banned after 3 infractions. With this reset, it can sustain indefinite misbehavior without ever being banned. [4](#0-3) 

### Likelihood Explanation

The discovery protocol is a standard P2P path. Any connected peer can broadcast `Nodes` messages containing arbitrary `Multiaddr` entries, triggering `add_addr` on the receiving node. No privilege is required. The precondition (existing entry with `last_connected_at_ms=0`) is the normal state for any peer learned via gossip rather than direct connection.

### Recommendation

In `AddrManager::add`, when an existing entry is found, preserve the existing `score` rather than overwriting it with the incoming value:

```rust
if addr_info.last_connected_at_ms >= exist_last_connected_at_ms {
    let existing_score = self.id_to_info.get(&id).expect("must exists").score;
    addr_info.random_id_pos = random_id_pos;
    addr_info.score = existing_score.min(addr_info.score); // keep lower score
    self.id_to_info.insert(id, addr_info);
}
```

Alternatively, `add_addr` should check whether an entry already exists and skip the insert entirely if the existing score has been penalized.

### Proof of Concept

```
1. peer_store.add_addr(addr_P, flags)
   → AddrInfo { last_connected_at_ms: 0, score: 100 }

2. peer_store.report(&addr_P, Behaviour::UnexpectedMessage)
   → score becomes e.g. 60 (above ban_score=40, no ban)

3. Remote peer sends discovery Nodes message containing addr_P
   → peer_store.add_addr(addr_P, flags) called again
   → AddrInfo::new(addr_P, 0, 100, flags)
   → AddrManager::add: 0 >= 0 → true → entry overwritten

4. peer_store.addr_manager.get(&addr_P).score == 100  ✓ (penalty erased)
``` [5](#0-4)

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

**File:** network/src/peer_store/peer_store_impl.rs (L153-167)
```rust
    pub fn report(&mut self, addr: &Multiaddr, behaviour: Behaviour) -> ReportResult {
        if let Some(peer_addr) = self.addr_manager.get_mut(addr) {
            let score = peer_addr.score.saturating_add(behaviour.score());
            peer_addr.score = score;
            if score < self.score_config.ban_score {
                self.ban_addr(
                    addr,
                    self.score_config.ban_timeout_ms,
                    format!("report behaviour {behaviour:?}"),
                );
                return ReportResult::Banned;
            }
        }
        ReportResult::Ok
    }
```

**File:** network/src/peer_store/addr_manager.rs (L22-34)
```rust
    pub fn add(&mut self, mut addr_info: AddrInfo) {
        if let Some(&id) = self.addr_to_id.get(&addr_info.addr) {
            let (exist_last_connected_at_ms, random_id_pos) = {
                let info = self.id_to_info.get(&id).expect("must exists");
                (info.last_connected_at_ms, info.random_id_pos)
            };
            // Get time earlier than record time, return directly
            if addr_info.last_connected_at_ms >= exist_last_connected_at_ms {
                addr_info.random_id_pos = random_id_pos;
                self.id_to_info.insert(id, addr_info);
            }
            return;
        }
```

**File:** network/src/peer_store/mod.rs (L51-58)
```rust
impl Default for PeerScoreConfig {
    fn default() -> Self {
        PeerScoreConfig {
            default_score: 100,
            ban_score: 40,
            ban_timeout_ms: 24 * 3600 * 1000, // 1 day
        }
    }
```
