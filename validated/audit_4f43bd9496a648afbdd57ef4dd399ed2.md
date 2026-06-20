### Title
Proxy Credentials and Tor Controller Password Exposed in Plaintext via Error Messages and Unredacted `Debug` Derive - (File: `network/src/proxy.rs`, `util/app-config/src/configs/network.rs`)

---

### Summary

`ProxyConfig` and `OnionConfig` both derive `Debug` without redacting their sensitive fields (`proxy_url` containing `socks5://username:password@...` and `tor_password`). Additionally, `check_proxy_url` constructs error messages that embed the full proxy URL — including any embedded credentials — in plaintext. These error strings propagate up through `NetworkState::from_config` and are surfaced to logs or stderr, exposing credentials to any party with access to the node's log output.

---

### Finding Description

**Root cause 1 — Unredacted `Debug` derive on credential-bearing config structs:**

`ProxyConfig` is declared with `#[derive(Clone, Debug, Serialize, Deserialize, Default)]` and holds `proxy_url: Option<String>`, documented as accepting values like `socks5://username:password@127.0.0.1:1080`. [1](#0-0) 

`OnionConfig` is similarly declared with `#[derive(Clone, Debug, ...)]` and holds `tor_password: Option<String>`. [2](#0-1) 

The parent `NetworkConfig` (struct `Config`) also derives `Debug` and embeds both of these structs. [3](#0-2) 

Any code path that formats `NetworkConfig`, `ProxyConfig`, or `OnionConfig` with `{:?}` — including panic messages, tracing spans, or debug-level log lines — will emit the full proxy URL (with embedded username and password) and the Tor controller password in plaintext.

**Root cause 2 — `check_proxy_url` embeds the full credential-bearing URL in error strings:**

`check_proxy_url` returns `Err(format!("missing host in proxy url: {}", proxy_url))` and `Err(format!("missing port in proxy url: {}", proxy_url))` when the URL is malformed. [4](#0-3) 

These error strings include the raw `proxy_url` value, which may contain `username:password`. The error is mapped to `Error::Config` and propagated with `?` from `NetworkState::from_config`. [5](#0-4) 

The resulting error message — containing the full credential string — is surfaced to the caller and ultimately printed to stderr or written to the node's log file on startup failure.

---

### Impact Explanation

An operator who configures a SOCKS5 proxy with embedded credentials (`socks5://user:pass@host:port`) or a Tor controller password will have those credentials written to:
- The node's log file (if debug logging is active or if the config struct is ever formatted with `{:?}`)
- stderr output on startup failure (via the `check_proxy_url` error path)

Log files are often world-readable or forwarded to centralized log aggregation systems. Any local user, log collector, or monitoring agent with read access to the log output can recover the proxy password or Tor controller password without any special privilege. In shared-server or containerized deployments this is a realistic lateral-movement vector.

---

### Likelihood Explanation

The `check_proxy_url` error path is triggered whenever a proxy URL is syntactically invalid (missing host or port), which is a common misconfiguration scenario. The `Debug` derive exposure is latent but persistent — it activates whenever any future log line or panic handler formats the config struct. Both paths require only that the operator has configured proxy credentials, which is the intended use case for the `proxy_url` field.

---

### Recommendation

1. Implement a manual `Debug` impl for `ProxyConfig` and `OnionConfig` that redacts `proxy_url` and `tor_password` (e.g., replace with `"[REDACTED]"`), analogous to how `HeaderValue::set_sensitive(true)` is already used for the HTTP Authorization header in the miner client. [6](#0-5) 

2. In `check_proxy_url`, strip credentials from the URL before including it in error messages (e.g., use `parsed_url` with password cleared, or replace the userinfo component with `***`). [4](#0-3) 

---

### Proof of Concept

Configure `ckb.toml` with:
```toml
[network.proxy]
proxy_url = "socks5://myuser:mysecretpass@"
```
(missing host — triggers the `check_proxy_url` error path)

Start the node. The startup error printed to stderr or the log file will contain:
```
missing host in proxy url: socks5://myuser:mysecretpass@
```

The plaintext password `mysecretpass` is now present in the log file and visible to any reader of that file.

### Citations

**File:** util/app-config/src/configs/network.rs (L20-114)
```rust
#[derive(Clone, Debug, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// Only connect to whitelist peers.
    #[serde(default)]
    pub whitelist_only: bool,
    /// Maximum number of allowed connected peers.
    ///
    /// The node will evict connections when the number exceeds this limit.
    pub max_peers: u32,
    /// Maximum number of outbound peers.
    ///
    /// When node A connects to B, B is the outbound peer of A.
    pub max_outbound_peers: u32,
    /// Network data storage directory path.
    #[serde(default)]
    pub path: PathBuf,
    /// A list of DNS servers to discover peers.
    #[serde(default)]
    pub dns_seeds: Vec<String>,
    /// Whether to probe and store local addresses.
    #[serde(default)]
    pub discovery_local_address: bool,
    /// The interval between discovery announce message checking.
    #[serde(default)]
    pub discovery_announce_check_interval_secs: Option<u64>,
    /// Interval between pings in seconds.
    ///
    /// A node pings peer regularly to see whether the connection is alive.
    pub ping_interval_secs: u64,
    /// The ping timeout in seconds.
    ///
    /// If a peer does not respond to ping before the timeout, it is evicted.
    pub ping_timeout_secs: u64,
    /// The interval between trials to connect more outbound peers.
    pub connect_outbound_interval_secs: u64,
    /// Listen addresses.
    pub listen_addresses: Vec<Multiaddr>,
    /// Public addresses.
    ///
    /// Set this if this is different from `listen_addresses`.
    #[serde(default)]
    pub public_addresses: Vec<Multiaddr>,
    /// A list of peers used to boot the node discovery.
    ///
    /// Bootnodes are used to bootstrap the discovery when local peer storage is empty.
    pub bootnodes: Vec<Multiaddr>,
    /// A list of peers added in the whitelist.
    ///
    /// When `whitelist_only` is enabled, the node will only connect to peers in this list.
    #[serde(default)]
    pub whitelist_peers: Vec<Multiaddr>,
    /// Enable UPNP when the router supports it.
    #[serde(default)]
    pub upnp: bool,
    /// Enable bootnode mode.
    ///
    /// It is recommended to enable this when this server is intended to be used as a node in the
    /// `bootnodes`.
    #[serde(default)]
    pub bootnode_mode: bool,
    /// Supported protocols list
    #[serde(default = "default_support_all_protocols")]
    pub support_protocols: Vec<SupportProtocol>,
    /// Max send buffer size in bytes.
    pub max_send_buffer: Option<usize>,
    /// Network use reuse port or not
    #[serde(default = "default_reuse")]
    pub reuse_port_on_linux: bool,
    /// Allow ckb to upgrade tcp listening to tcp + ws listening
    #[serde(default = "default_reuse_tcp_with_ws")]
    pub reuse_tcp_with_ws: bool,
    /// Disable block_relay_only connection, only use for testing.
    #[serde(default)]
    pub disable_block_relay_only_connection: bool,
    /// Tentacle inner channel_size.
    pub channel_size: Option<usize>,
    /// A list of trusted proxies' IP addresses.
    #[serde(default = "default_trusted_proxies")]
    pub trusted_proxies: Vec<IpAddr>,
    #[cfg(target_family = "wasm")]
    #[serde(skip)]
    pub secret_key: [u8; 32],
    /// Chain synchronization config options.
    #[serde(default)]
    pub sync: SyncConfig,

    /// Proxy related config options
    #[serde(default)]
    pub proxy: ProxyConfig,

    /// Onion related config options
    #[serde(default)]
    pub onion: OnionConfig,
}
```

**File:** util/app-config/src/configs/network.rs (L117-124)
```rust
#[derive(Clone, Debug, Serialize, Deserialize, Default)]
pub struct ProxyConfig {
    // like: socks5://username:password@127.0.0.1:1080
    pub proxy_url: Option<String>,
    // use random auth for each proxy connection
    #[serde(default = "default_proxy_random_auth")]
    pub proxy_random_auth: bool,
}
```

**File:** util/app-config/src/configs/network.rs (L132-155)
```rust
#[derive(Clone, Debug, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct OnionConfig {
    // Automatically create Tor onion service
    pub listen_on_onion: bool,
    // Tor server url: like: 127.0.0.1:9050
    pub onion_server: Option<String>,
    // The onion service will proxy incoming traffic to `p2p_listen_address`.
    // If the CKB's peer-to-peer listen address is not set to the default 127.0.0.1
    // with the port specified in `[network].listen_addresses` for IPv4, you should configure this field.
    pub p2p_listen_address: Option<String>,
    // path to store onion private key, default is ./data/network/onion_private_key
    pub onion_private_key_path: Option<String>,
    // tor controller url, example: 127.0.0.1:9051
    #[serde(default = "default_tor_controller")]
    pub tor_controller: String,
    // tor controller hashed password
    pub tor_password: Option<String>,
    // The external port that the onion service will expose. Default is 8115.
    // This is the port that will be advertised in the onion address,
    // while traffic will be forwarded to `p2p_listen_address`.
    #[serde(default = "default_onion_external_port")]
    pub onion_external_port: u16,
}
```

**File:** network/src/proxy.rs (L3-16)
```rust
pub(crate) fn check_proxy_url(proxy_url: &str) -> Result<(), String> {
    let parsed_url = Url::parse(proxy_url).map_err(|e| e.to_string())?;
    if parsed_url.host_str().is_none() {
        return Err(format!("missing host in proxy url: {}", proxy_url));
    }
    let scheme = parsed_url.scheme();
    if scheme.ne("socks5") {
        return Err(format!("CKB doesn't support proxy scheme: {}", scheme));
    }
    if parsed_url.port().is_none() {
        return Err(format!("missing port in proxy url: {}", proxy_url));
    }
    Ok(())
}
```

**File:** network/src/network.rs (L129-131)
```rust
        if let Some(ref proxy_url) = config.proxy.proxy_url {
            proxy::check_proxy_url(proxy_url).map_err(Error::Config)?;
        }
```

**File:** miner/src/client.rs (L388-390)
```rust
        let mut header = HeaderValue::from_str(&encoded).unwrap();
        header.set_sensitive(true);
        Some(header)
```
