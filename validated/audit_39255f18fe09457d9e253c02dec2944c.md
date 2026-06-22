### Title
Tor Uptime Value Fetched But Never Inspected — Stale Onion Address Permanently Advertised After Tor Service Disruption - (File: util/onion/src/onion_service.rs)

### Summary

`OnionService::launch_onion_service` spawns a background task that calls `get_uptime()` every 3 seconds as a liveness probe, but the returned `Duration` value is never inspected — only the error/success result is checked. Separately, `add_public_addr` is called once when the onion service is registered and there is no corresponding removal path. If the Tor daemon restarts quickly enough that the TCP control connection survives (or if the onion service is deregistered without the control connection dropping), the node continues to advertise the stale onion address to all peers indefinitely, while the actual onion service is gone.

### Finding Description

**Root cause — uptime value silently discarded:**

In `launch_onion_service`, a background task polls `get_uptime()` every 3 seconds:

```rust
let uptime = tor_controller.get_uptime().await;
if let Err(err) = uptime {
    error!("Failed to get tor server uptime: {:?}", err);
    drop(tor_server_alive_tx);
    return;
}
// Ok(Duration) branch: value is completely ignored
``` [1](#0-0) 

The `Ok(Duration)` branch does nothing. The actual uptime value — which would reveal a near-zero value after a Tor restart — is never read.

**Root cause — no removal path for stale public address:**

When `launch_onion_service` succeeds, the onion address is permanently inserted into `NetworkState::public_addrs`:

```rust
Ok(_) => {
    network_controller.add_public_addr(onion_service_addr.clone());
}
``` [2](#0-1) 

`add_public_addr` inserts into a `HashSet` with no corresponding remove operation: [3](#0-2) 

The `public_addrs` set is read by `public_urls`, which feeds the Identify protocol and the `local_node_info` RPC — both of which propagate the address to all connected peers: [4](#0-3) 

**Exploit path:**

1. CKB node operator enables `listen_on_onion = true`.
2. Onion service registers successfully; `add_public_addr` is called; the address is gossiped to all peers via the Identify protocol.
3. The Tor daemon restarts quickly (e.g., system update, OOM kill, watchdog restart). If the OS TCP stack has not yet torn down the existing control connection (within the 3-second polling window), `get_uptime()` returns `Ok(Duration::from_secs(~0))` — success — and the background task does not drop `tor_server_alive_tx`.
4. The non-persistent onion service (registered with `detach=false`) is gone from Tor's internal state, but the CKB node has no way to know this because the uptime value is never inspected.
5. The stale onion address remains in `public_addrs` and continues to be advertised to every peer that connects. [5](#0-4) 

The `add_onion_v3` call uses `detach=false`, meaning the service is tied to the control connection — but only the connection *drop* is detected, not a silent restart where the connection briefly survives: [6](#0-5) 

### Impact Explanation

Peers that receive the stale onion address via the Identify protocol will attempt to connect and fail. Because `public_addrs` has no removal path, the stale address persists for the entire lifetime of the CKB process (or until the next successful re-registration overwrites it, which requires the control connection to fully drop first). A CKB node running in Tor-only mode (no clearnet listen address) would be unreachable to all peers that attempt to connect via the advertised onion address, effectively isolating it from the P2P network. This degrades block and transaction propagation for that node.

### Likelihood Explanation

Tor daemon restarts are common operational events (package upgrades, OOM kills, systemd watchdog restarts). The race window is 3 seconds (the polling interval). On Linux, a TCP connection to a local socket (127.0.0.1:9051) may not be detected as broken by the application layer for several seconds after the peer process exits, making the race realistic. Additionally, the comment in `get_uptime()` explicitly acknowledges that older Tor versions do not expose the `uptime` command at all — in that case `get_uptime()` always returns `Err`, the background task immediately drops `tor_server_alive_tx`, and the retry loop fires continuously, repeatedly registering and deregistering the onion service while the stale address remains in `public_addrs` throughout. [7](#0-6) 

### Recommendation

1. **Inspect the uptime value**: After a successful `get_uptime()` call, compare the returned `Duration` against the previously observed value. If the uptime has decreased (indicating a restart), treat it as a liveness failure and drop `tor_server_alive_tx` to trigger re-registration.

2. **Remove the stale address on failure**: Add a `remove_public_addr` method to `NetworkState` / `NetworkController` and call it in the `Err` branch of `start()` before retrying, so the stale onion address is no longer advertised to peers during the re-registration window.

3. **Verify onion service registration directly**: Instead of (or in addition to) polling `get_uptime()`, periodically call `GETINFO onions/current` via the Tor control protocol to confirm the onion service is still registered.

### Proof of Concept

```
1. Configure CKB with listen_on_onion = true, tor_controller = "127.0.0.1:9051".
2. Start CKB. Observe onion address added via add_public_addr (log: "CKB has started listening on the onion hidden network").
3. Verify address is advertised: call local_node_info RPC, confirm /onion3/... address present.
4. Kill and immediately restart the Tor daemon (systemctl restart tor).
5. Within 3 seconds of restart, call local_node_info RPC again.
6. Observe: /onion3/... address is still present in the response.
7. Attempt to connect to the CKB node via the advertised onion address from a remote peer: connection fails (onion service no longer registered in Tor).
8. The stale address remains advertised until the background task detects the connection drop (which may not happen if the TCP connection survives the restart window).
``` [8](#0-7)

### Citations

**File:** util/onion/src/onion_service.rs (L60-96)
```rust
    /// Start the onion service.
    pub async fn start(
        &self,
        network_controller: NetworkController,
        onion_service_addr: Multiaddr,
    ) -> Result<(), Error> {
        let stop_rx = ckb_stop_handler::new_tokio_exit_rx();
        loop {
            let (tor_server_alive_tx, mut tor_server_alive_rx) =
                tokio::sync::mpsc::unbounded_channel::<()>();
            match self
                .launch_onion_service(stop_rx.clone(), tor_server_alive_tx)
                .await
            {
                Ok(_) => {
                    info!(
                        "CKB has started listening on the onion hidden network, the onion service address is: {}",
                        onion_service_addr.clone()
                    );
                    network_controller.add_public_addr(onion_service_addr.clone());
                }
                Err(err) => {
                    error!("start onion service failed: {}", err);
                }
            }

            let _ = tor_server_alive_rx.recv().await;
            if stop_rx.is_cancelled() {
                return Ok(());
            }
            warn!(
                "It seems that the connection to tor server's controller has been closed, retry connect to tor controller({})",
                self.config.tor_controller.to_string()
            );
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
    }
```

**File:** util/onion/src/onion_service.rs (L130-148)
```rust
        self.handle.spawn(async move {
            let mut ticker = tokio::time::interval(tokio::time::Duration::from_secs(3));
            loop {
                tokio::select! {
                    _ = ticker.tick() => {
                        let uptime = tor_controller.get_uptime().await;
                        if let Err(err) = uptime {
                            error!("Failed to get tor server uptime: {:?}", err);
                            drop(tor_server_alive_tx);
                            return;
                        }
                    }
                    _ = stop_rx.cancelled() => {
                        info!("OnionService received stop signal, exiting...");
                        drop(tor_server_alive_tx);
                        return;
                    }
                }
            }
```

**File:** network/src/network.rs (L352-356)
```rust
    /// After onion service created,
    /// ckb use this method to add onion address to public_addr
    pub fn add_public_addr(&self, addr: Multiaddr) {
        self.public_addrs.write().insert(addr);
    }
```

**File:** network/src/protocols/identify/mod.rs (L211-232)
```rust
        let listen_addrs = if self.callback.register(&context, version) {
            Vec::new()
        } else {
            self.callback
                .local_listen_addrs()
                .iter()
                .filter(|addr| {
                    if let Some(socket_addr) = multiaddr_to_socketaddr(addr) {
                        !self.global_ip_only || is_reachable(socket_addr.ip())
                    } else {
                        // allow /onion3 address
                        addr.iter()
                            .any(|protocol| matches!(protocol, Protocol::Onion3(_)))
                    }
                })
                .take(MAX_ADDRS)
                .cloned()
                .collect()
        };

        let identify = self.callback.identify();
        let data = IdentifyMessage::new(listen_addrs, session.address.clone(), identify).encode();
```

**File:** util/onion/src/tor_controller.rs (L65-82)
```rust
    pub async fn get_uptime(&mut self) -> Result<Duration, ConnError> {
        let uptime = self.inner.get_info("uptime").await.map_err(|err| {
            // the tor server's version is less than 0.3.5.1-alpha
            warn!(
                "failed to get uptime; the Tor controller may not expose 'uptime' (older Tor versions) or returned an error: {}",
                err
            );
            err
        })?;
        debug!("tor server's uptime is {} seconds", uptime);
        let secs: u64 = uptime.parse().map_err(|err| {
            ConnError::IOError(std::io::Error::other(format!(
                "failed to parse uptime {} to u64 {}",
                uptime, err
            )))
        })?;
        Ok(Duration::from_secs(secs))
    }
```

**File:** util/onion/src/tor_controller.rs (L116-124)
```rust
    pub async fn add_onion_v3(
        &mut self,
        key: TorSecretKeyV3,
        listeners: &mut impl Iterator<Item = &(u16, SocketAddr)>,
    ) -> Result<(), torut::control::ConnError> {
        self.inner
            .add_onion_v3(&key, false, false, false, None, listeners)
            .await
    }
```
