### Title
Plaintext Secrets Retained in Memory with `Debug`-Derived Config Structs — (`File: util/app-config/src/configs/network.rs`)

---

### Summary

`OnionConfig`, `ProxyConfig`, and (on the wasm target) `NetworkConfig` all derive `Debug` while holding sensitive secrets as plain `String` or `[u8; 32]` fields. These secrets — the Tor controller password, proxy URL with embedded credentials, and the raw P2P identity private key — are retained in process memory for the entire lifetime of the node and are trivially serialisable to plaintext via Rust's `{:?}` formatter.

---

### Finding Description

Three production config structs in `util/app-config/src/configs/network.rs` derive `Debug` while containing sensitive material:

**1. `OnionConfig` — Tor controller password**

```rust
#[derive(Clone, Debug, Serialize, Deserialize, Default)]
pub struct OnionConfig {
    // tor controller hashed password
    pub tor_password: Option<String>,
    ...
}
```

`tor_password` is a plaintext `String` stored for the node's lifetime. [1](#0-0) 

**2. `ProxyConfig` — proxy URL with embedded credentials**

```rust
#[derive(Clone, Debug, Serialize, Deserialize, Default)]
pub struct ProxyConfig {
    // like: socks5://username:password@127.0.0.1:1080
    pub proxy_url: Option<String>,
    ...
}
```

The comment explicitly documents that `proxy_url` may contain `username:password` in the URL. [2](#0-1) 

**3. `NetworkConfig` (wasm target) — raw P2P identity private key**

```rust
#[derive(Clone, Debug, Serialize, Deserialize, Default)]
pub struct Config {
    ...
    #[cfg(target_family = "wasm")]
    #[serde(skip)]
    pub secret_key: [u8; 32],
    ...
}
```

The node's secp256k1 P2P identity private key is stored as a raw `[u8; 32]` in a public field of a struct that derives `Debug`. [3](#0-2) 

All three structs are nested inside `NetworkConfig`, which is itself a field of `CKBAppConfig`:

```rust
#[derive(Clone, Debug, Serialize)]
pub struct CKBAppConfig {
    pub network: NetworkConfig,
    ...
}
``` [4](#0-3) 

This means a single `format!("{:?}", app_config)` anywhere in the call stack — in error handlers, panic hooks, diagnostic logging, or future developer additions — dumps all three secrets to plaintext output.

By contrast, the `Privkey` type used for signing does implement proper zeroization via `Drop`:

```rust
impl Drop for Privkey {
    fn drop(&mut self) { self.zeroize() }
}
``` [5](#0-4) 

No equivalent protection exists for `tor_password`, `proxy_url`, or the wasm `secret_key` field.

---

### Impact Explanation

- **`tor_password`**: Exposure allows an attacker who can read logs or memory to authenticate to the Tor controller, add/remove onion services, and deanonymize the node's hidden-service identity.
- **`proxy_url` credentials**: Exposure allows an attacker to reuse the SOCKS5 credentials to route arbitrary traffic through the operator's proxy, potentially incurring cost or enabling further lateral movement.
- **`secret_key` (wasm)**: Exposure of the raw 32-byte P2P identity private key allows an attacker to impersonate the node on the P2P network, forge its peer identity, and disrupt its connections or perform eclipse attacks against peers that trust it.

The `NetworkState` struct stores `local_private_key: secio::SecioKeyPair` for the node's entire runtime, and `OnionServiceConfig` also carries `tor_password: Option<String>` independently:

```rust
pub struct OnionServiceConfig {
    pub tor_password: Option<String>,
    ...
}
``` [6](#0-5) 

---

### Likelihood Explanation

Medium. The `Debug` derive on `CKBAppConfig` and its nested structs is a latent exposure path. Any future log statement, panic handler, or diagnostic tool that formats the config with `{:?}` will silently leak all three secrets. The secrets also remain as plaintext heap allocations (`String`, `Vec<u8>`) for the full process lifetime, widening the window for memory-disclosure attacks (e.g., core dumps, `/proc/self/mem` reads by a co-located process, or swap-file leakage). The wasm `secret_key` field is the most severe because it is a private key with no zeroization on drop.

---

### Recommendation

1. Remove `Debug` from `OnionConfig`, `ProxyConfig`, and `NetworkConfig` (or implement a redacting `Debug` that prints `[REDACTED]` for sensitive fields).
2. Replace `tor_password: Option<String>` and `proxy_url: Option<String>` with a newtype wrapper that implements `Drop` with explicit zeroing and overrides `Debug`/`Display` to suppress the value.
3. For the wasm `secret_key: [u8; 32]`, replace the raw array with a zeroizing wrapper (e.g., `zeroize::Zeroizing<[u8; 32]>`) and remove the `Debug` derive or redact the field.
4. Audit all call sites that pass `AppConfig`, `NetworkConfig`, or `OnionServiceConfig` to logging macros.

---

### Proof of Concept

```rust
// Reproduces the Debug leak for tor_password
use ckb_app_config::configs::network::{OnionConfig};

let cfg = OnionConfig {
    tor_password: Some("s3cr3t_tor_pass".to_string()),
    ..Default::default()
};
// Prints: OnionConfig { ..., tor_password: Some("s3cr3t_tor_pass"), ... }
println!("{:?}", cfg);
```

Because `OnionConfig` is a field of `NetworkConfig` which is a field of `CKBAppConfig`, the same leak occurs whenever `CKBAppConfig` is debug-formatted. [7](#0-6)

### Citations

**File:** util/app-config/src/configs/network.rs (L100-103)
```rust
    #[cfg(target_family = "wasm")]
    #[serde(skip)]
    pub secret_key: [u8; 32],
    /// Chain synchronization config options.
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

**File:** util/app-config/src/app_config.rs (L38-98)
```rust
#[derive(Clone, Debug, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CKBAppConfig {
    /// The binary name.
    #[serde(skip)]
    pub bin_name: String,
    /// The root directory.
    #[serde(skip)]
    pub root_dir: PathBuf,
    /// The data directory.
    pub data_dir: PathBuf,
    /// freezer files path
    #[serde(default)]
    pub ancient: PathBuf,
    /// The directory to store temporary files.
    pub tmp_dir: Option<PathBuf>,
    /// Logger config options.
    pub logger: LogConfig,
    /// Sentry config options.
    #[cfg(feature = "with_sentry")]
    #[serde(default)]
    pub sentry: SentryConfig,
    /// Metrics options.
    ///
    /// Developers can collect metrics for performance tuning and troubleshooting.
    #[serde(default)]
    pub metrics: MetricsConfig,
    /// Memory tracker options.
    ///
    /// Developers can enable memory tracker to analyze the process memory usage.
    #[serde(default)]
    pub memory_tracker: MemoryTrackerConfig,
    /// Chain config options.
    pub chain: ChainConfig,

    /// Block assembler options.
    pub block_assembler: Option<BlockAssemblerConfig>,
    /// Database config options.
    #[serde(default)]
    pub db: DBConfig,
    /// Network config options.
    pub network: NetworkConfig,
    /// RPC config options.
    pub rpc: RpcConfig,
    /// Tx pool config options.
    pub tx_pool: TxPoolConfig,
    /// Store config options.
    #[serde(default)]
    pub store: StoreConfig,
    /// P2P alert config options.
    pub alert_signature: Option<NetworkAlertConfig>,
    /// Notify config options.
    #[serde(default)]
    pub notify: NotifyConfig,
    /// Indexer config options.
    #[serde(default)]
    pub indexer: IndexerConfig,
    /// Fee estimator config options.
    #[serde(default)]
    pub fee_estimator: FeeEstimatorConfig,
}
```

**File:** util/crypto/src/secp/privkey.rs (L93-97)
```rust
impl Drop for Privkey {
    fn drop(&mut self) {
        self.zeroize()
    }
}
```

**File:** util/onion/src/lib.rs (L19-33)
```rust
pub struct OnionServiceConfig {
    /// path to store onion private key, default is ./data/network/onion_private_key
    pub onion_private_key_path: String,
    /// tor controller url, example: 127.0.0.1:9051
    pub tor_controller: String,
    /// tor controller hashed password
    pub tor_password: Option<String>,
    /// onion service will bind to CKB's p2p listen address, default is "127.0.0.1:8115"
    /// if you want to use other address, you should set it to the address you want
    pub p2p_listen_address: SocketAddr,
    /// The external port that the onion service will expose, default is 8115
    /// This is the port that will be advertised in the onion address,
    /// while traffic will be forwarded to `p2p_listen_address`.
    pub onion_external_port: u16,
}
```
