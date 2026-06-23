### Title
Missing Log/Notification Emission for Critical Ban-List Mutations — (`rpc/src/module/net.rs`, `network/src/network.rs`)

---

### Summary

The `set_ban` and `clear_banned_addresses` RPC handlers mutate the node's peer ban list — a security-critical network state — without emitting any log message or `NotifyController` notification. This is a direct analog to the reported "lack of events for critical operations" pattern: an operator or monitoring system has no observable signal that the ban list was changed, making silent tampering undetectable.

---

### Finding Description

CKB's `NotifyController` (`notify/src/lib.rs`) provides a publish-subscribe system for blockchain events: new blocks, new/proposed/rejected transactions, network alerts, and log entries. There is no notification type for ban-list mutations.

The two affected RPC handlers are:

**`clear_banned_addresses`** (`rpc/src/module/net.rs:686–689`):
```rust
fn clear_banned_addresses(&self) -> Result<()> {
    self.network_controller.clear_banned_addrs();
    Ok(())
}
```
No `info!`, `warn!`, or `notify_*` call is present.

**`set_ban` — insert branch** (`rpc/src/module/net.rs:706–717`):
```rust
"insert" => {
    ...
    self.network_controller
        .ban(ip_network, ban_until, reason.unwrap_or_default());
    Ok(())
}
```

**`set_ban` — delete branch** (`rpc/src/module/net.rs:719–721`):
```rust
"delete" => {
    self.network_controller.unban(&ip_network);
    Ok(())
}
```

The underlying `NetworkController` methods are equally silent:

- `NetworkController::ban()` (`network/src/network.rs:1422–1428`) — calls `disconnect_peers_in_ip_range` and `ban_network` with no log.
- `NetworkController::unban()` (`network/src/network.rs:1431–1437`) — calls `unban_network` with no log.
- `NetworkController::clear_banned_addrs()` (`network/src/network.rs:1407–1409`) — calls `clear_ban_list` with no log.

The storage layer `BanList::ban()` and `BanList::unban_network()` (`network/src/peer_store/ban_list.rs:34–46`) also contain no logging.

---

### Impact Explanation

The peer ban list is the node's primary defense against known-malicious peers. Silently clearing it (`clear_banned_addresses`) immediately re-admits every previously-banned IP/subnet. Silently inserting a ban (`set_ban insert`) can censor legitimate peers. Neither action leaves any trace in the node's log stream or notification channel.

A node operator running a monitoring system subscribed to `NotifyController` events has no way to detect that the ban list was modified. There is no audit trail for ban-list history. If an attacker with local RPC access clears the ban list, previously-banned malicious peers can reconnect and exploit any other protocol-level vulnerability without the operator being alerted.

---

### Likelihood Explanation

The RPC endpoint defaults to `127.0.0.1:8114` (`resource/ckb.toml:182`), so exploitation requires local access. However:
- The prompt explicitly includes "supported local CLI/RPC user" as an in-scope attacker.
- Some operators expose the RPC port to broader networks.
- Legitimate operators calling `set_ban` or `clear_banned_addresses` also produce no audit record, so even accidental misuse is invisible.

---

### Recommendation

**Short term:** Add structured log entries (`info!` or `warn!`) at every ban-list mutation site — at minimum in `NetRpcImpl::set_ban` and `NetRpcImpl::clear_banned_addresses` — recording the address, command, reason, and caller context.

**Long term:** Extend `NotifyController` with a `ban_list_changed` event type so that external monitoring scripts (analogous to `new_block_notify_script`) can react to ban-list mutations in real time.

---

### Proof of Concept

1. Start a CKB node with default config.
2. Call `set_ban("1.2.3.4", "insert", null, null, "test")` via RPC.
3. Call `clear_banned_addresses()` via RPC.
4. Inspect the node log and the `NotifyController` subscription stream — neither operation produces any log line or notification event.

**Root cause chain:**

`NetRpcImpl::set_ban` / `clear_banned_addresses` [1](#0-0) [2](#0-1) 

→ `NetworkController::ban` / `unban` / `clear_banned_addrs` [3](#0-2) 

→ `BanList::ban` / `unban_network` (no logging at any layer) [4](#0-3) 

`NotifyController` event types — no ban-list event exists: [5](#0-4)

### Citations

**File:** rpc/src/module/net.rs (L686-689)
```rust
    fn clear_banned_addresses(&self) -> Result<()> {
        self.network_controller.clear_banned_addrs();
        Ok(())
    }
```

**File:** rpc/src/module/net.rs (L705-727)
```rust
        match command.as_ref() {
            "insert" => {
                let ban_until = if absolute.unwrap_or(false) {
                    ban_time.unwrap_or_default().into()
                } else {
                    unix_time_as_millis()
                        + ban_time
                            .unwrap_or_else(|| DEFAULT_BAN_DURATION.into())
                            .value()
                };
                self.network_controller
                    .ban(ip_network, ban_until, reason.unwrap_or_default());
                Ok(())
            }
            "delete" => {
                self.network_controller.unban(&ip_network);
                Ok(())
            }
            _ => Err(RPCError::invalid_params(format!(
                "Expected `params[1]` to be in the list [insert, delete], got {address}"
            ))),
        }
    }
```

**File:** network/src/network.rs (L1407-1437)
```rust
    pub fn clear_banned_addrs(&self) {
        self.network_state.peer_store.lock().clear_ban_list();
    }

    /// Get address info from peer store
    pub fn addr_info(&self, addr: &Multiaddr) -> Option<AddrInfo> {
        self.network_state
            .peer_store
            .lock()
            .addr_manager()
            .get(addr)
            .cloned()
    }

    /// Ban an ip
    pub fn ban(&self, address: IpNetwork, ban_until: u64, ban_reason: String) {
        self.disconnect_peers_in_ip_range(address, &ban_reason);
        self.network_state
            .peer_store
            .lock()
            .ban_network(address, ban_until, ban_reason)
    }

    /// Unban an ip
    pub fn unban(&self, address: &IpNetwork) {
        self.network_state
            .peer_store
            .lock()
            .mut_ban_list()
            .unban_network(address);
    }
```

**File:** network/src/peer_store/ban_list.rs (L34-46)
```rust
    pub fn ban(&mut self, banned_addr: BannedAddr) {
        self.inner.insert(banned_addr.address, banned_addr);
        let (insert_count, _) = self.insert_count.overflowing_add(1);
        self.insert_count = insert_count;
        if self.insert_count.is_multiple_of(CLEAR_INTERVAL_COUNTER) {
            self.clear_expires();
        }
    }

    /// Unban address
    pub fn unban_network(&mut self, ip_network: &IpNetwork) {
        self.inner.remove(ip_network);
    }
```

**File:** notify/src/lib.rs (L110-125)
```rust
pub struct NotifyController {
    new_block_register: NotifyRegister<BlockView>,
    new_block_watcher: NotifyWatcher<Byte32>,
    new_block_notifier: Sender<BlockView>,
    new_transaction_register: NotifyRegister<PoolTransactionEntry>,
    new_transaction_notifier: Sender<PoolTransactionEntry>,
    proposed_transaction_register: NotifyRegister<PoolTransactionEntry>,
    proposed_transaction_notifier: Sender<PoolTransactionEntry>,
    reject_transaction_register: NotifyRegister<(PoolTransactionEntry, Reject)>,
    reject_transaction_notifier: Sender<(PoolTransactionEntry, Reject)>,
    network_alert_register: NotifyRegister<Alert>,
    network_alert_notifier: Sender<Alert>,
    log_register: NotifyRegister<LogEntry>,
    log_notifier: Sender<LogEntry>,
    handle: Handle,
}
```
