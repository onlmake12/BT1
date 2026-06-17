### Title
Plaintext Private Key Exposure via Unredacted `Debug` and `Serialize` Derives on `SecretString` — (File: apps/fortuna/src/config.rs)

---

### Summary
The `SecretString` struct used to hold Fortuna's keeper, provider, and fee-manager private keys derives `Debug` and `serde::Serialize` without any custom redaction. Any debug-formatted log line, panic message, or serialization of the parent config structs (`Config`, `ProviderConfig`, `KeeperConfig`) will emit the raw private key in plaintext. The same pattern is duplicated in Argus (`apps/argus/src/config.rs`).

---

### Finding Description

`SecretString` is defined as:

```rust
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct SecretString {
    pub value: Option<String>,
    pub file: Option<String>,
}
``` [1](#0-0) 

Every struct that embeds `SecretString` also derives `Debug`:

- `ProviderConfig` — holds `private_key: SecretString` and `secret: SecretString`
- `KeeperConfig` — holds `private_key: SecretString` and `fee_manager_private_key: Option<SecretString>`
- `Config` — the top-level config struct [2](#0-1) [3](#0-2) 

Because `Debug` is auto-derived, any `{:?}` formatting of these structs — in a `tracing` log, a `panic!`, an `anyhow` error chain, or a test assertion — will print the raw key value. The `serde::Serialize` derive additionally means the key is emitted in plaintext if the config is ever serialized to JSON/YAML (e.g., for diagnostics, health endpoints, or config-dump utilities).

The identical pattern exists in Argus: [4](#0-3) 

Additionally, the `GenerateOptions` struct accepts the private key directly as a CLI argument (`--private-key`) or environment variable (`PRIVATE_KEY`), making it visible in process lists and shell history: [5](#0-4) 

---

### Impact Explanation
If an attacker gains read access to application logs, a crash report, a log-aggregation pipeline, or a cloud provider console, they can extract:
- The **keeper private key** — used to submit entropy callback transactions on-chain
- The **provider private key** — used to register and manage the Entropy provider
- The **fee manager private key** — used to withdraw accrued fees from the Entropy contract

Possession of any of these keys allows direct on-chain fund theft or provider impersonation.

---

### Likelihood Explanation
Low. Exploitation requires secondary access to logs or process metadata (e.g., a misconfigured log aggregator, a cloud IAM misconfiguration, or a co-located process). However, the root cause is entirely within Pyth's own code and is passively triggered by normal operational events (errors, panics, debug logging) rather than requiring any attacker-controlled input.

---

### Recommendation
Implement a custom `Debug` (and optionally `Display`) for `SecretString` that always prints a redacted placeholder:

```rust
impl fmt::Debug for SecretString {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SecretString")
            .field("value", &self.value.as_ref().map(|_| "<redacted>"))
            .field("file", &self.file)
            .finish()
    }
}
```

Remove `serde::Serialize` from `SecretString` or implement it to redact the `value` field. For CLI commands, prefer reading the private key from a file path argument rather than accepting the raw key as a `--private-key` argument.

---

### Proof of Concept
Any error path that propagates the config struct will expose the key. For example, if `Config::load` returns an `Err` that is logged with `{:?}`, or if a keeper thread panics and the panic handler prints the `KeeperConfig`, the raw private key stored under `value` is emitted to stderr/logs verbatim. No attacker interaction is required beyond triggering a normal operational error condition. [6](#0-5) [7](#0-6)

### Citations

**File:** apps/fortuna/src/config.rs (L74-115)
```rust
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct Config {
    pub chains: HashMap<ChainId, EthereumConfig>,
    pub provider: ProviderConfig,
    pub keeper: KeeperConfig,
}

impl Config {
    pub fn load(path: &str) -> Result<Config> {
        // Open and read the YAML file
        // TODO: the default serde deserialization doesn't enforce unique keys
        let yaml_content = fs::read_to_string(path)?;
        let config: Config = serde_yaml::from_str(&yaml_content)?;

        // Run correctness checks for the config and fail if there are any issues.
        for (chain_id, config) in config.chains.iter() {
            if !(config.min_profit_pct <= config.target_profit_pct
                && config.target_profit_pct <= config.max_profit_pct)
            {
                return Err(anyhow!("chain id {:?} configuration is invalid. Config must satisfy min_profit_pct <= target_profit_pct <= max_profit_pct.", chain_id));
            }
        }

        if let Some(replica_config) = &config.keeper.replica_config {
            if replica_config.total_replicas == 0 {
                return Err(anyhow!("Keeper replica configuration is invalid. total_replicas must be greater than 0."));
            }
            if config.keeper.private_key.load()?.is_none() {
                return Err(anyhow!(
                    "Keeper replica configuration requires a keeper private key to be specified."
                ));
            }
            if replica_config.replica_id >= replica_config.total_replicas {
                return Err(anyhow!("Keeper replica configuration is invalid. replica_id must be less than total_replicas."));
            }
            if replica_config.backup_delay_seconds == 0 {
                return Err(anyhow!("Keeper replica configuration is invalid. backup_delay_seconds must be greater than 0 to prevent race conditions."));
            }
        }

        Ok(config)
    }
```

**File:** apps/fortuna/src/config.rs (L366-395)
```rust
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct ProviderConfig {
    /// The URI where clients can retrieve random values from this provider,
    /// i.e., wherever fortuna for this provider will be hosted.
    pub uri: String,

    /// The public key of the provider whose requests the server will respond to.
    pub address: Address,

    /// The provider's private key, which is required to register, update the commitment,
    /// or claim fees. This argument *will not* be loaded for commands that do not need
    /// the private key (e.g., running the server).
    pub private_key: SecretString,

    /// The provider's secret which is a 64-char hex string.
    /// The secret is used for generating new hash chains
    pub secret: SecretString,

    /// The length of the hash chain to generate.
    pub chain_length: u64,

    /// How frequently the hash chain is sampled -- increase this value to tradeoff more
    /// compute per request for less RAM use.
    #[serde(default = "default_chain_sample_interval")]
    pub chain_sample_interval: u64,

    /// The address of the fee manager for the provider. Only used for syncing the fee manager address to the contract.
    /// Fee withdrawals are handled by the fee manager private key defined in the keeper config.
    pub fee_manager: Option<Address>,
}
```

**File:** apps/fortuna/src/config.rs (L421-443)
```rust
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct KeeperConfig {
    /// If provided, the keeper will run alongside the Fortuna API service.
    /// The private key is a 20-byte (40 char) hex encoded Ethereum private key.
    /// This key is required to submit transactions for entropy callback requests.
    /// This key *does not need to be a registered provider*. In particular, production deployments
    /// should ensure this is a different key in order to reduce the severity of security breaches.
    pub private_key: SecretString,

    /// The fee manager's private key for fee manager operations.
    /// This key is used to withdraw fees from the contract as the fee manager.
    /// Multiple replicas can share the same fee manager private key but different keeper keys (`private_key`).
    #[serde(default)]
    pub fee_manager_private_key: Option<SecretString>,

    /// The addresses of other keepers in the replica set (excluding the current keeper).
    /// This is used to distribute fees fairly across all keepers.
    #[serde(default)]
    pub other_keeper_addresses: Vec<Address>,

    #[serde(default)]
    pub replica_config: Option<ReplicaConfig>,
}
```

**File:** apps/fortuna/src/config.rs (L447-454)
```rust
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct SecretString {
    pub value: Option<String>,

    // The name of a file containing the string to read. Note that the file contents is trimmed
    // of leading/trailing whitespace when read.
    pub file: Option<String>,
}
```

**File:** apps/argus/src/config.rs (L219-240)
```rust
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct SecretString {
    pub value: Option<String>,

    // The name of a file containing the string to read. Note that the file contents is trimmed
    // of leading/trailing whitespace when read.
    pub file: Option<String>,
}

impl SecretString {
    pub fn load(&self) -> Result<Option<String>> {
        if let Some(v) = &self.value {
            return Ok(Some(v.clone()));
        }

        if let Some(v) = &self.file {
            return Ok(Some(fs::read_to_string(v)?.trim().to_string()));
        }

        Ok(None)
    }
}
```

**File:** apps/fortuna/src/config/generate.rs (L19-24)
```rust
    /// A 20-byte (40 char) hex encoded Ethereum private key.
    /// This key is required to submit transactions (such as registering with the contract).
    #[arg(long = "private-key")]
    #[arg(env = "PRIVATE_KEY")]
    #[arg(default_value = None)]
    pub private_key: String,
```

**File:** apps/fortuna/src/command/run.rs (L91-108)
```rust
pub async fn run(opts: &RunOptions) -> Result<()> {
    // Load environment variables from a .env file if present
    let _ = dotenv::dotenv().map_err(|e| anyhow!("Failed to load .env file: {}", e))?;
    let config = Config::load(&opts.config.config)?;
    let secret = config.provider.secret.load()?.ok_or(anyhow!(
        "Please specify a provider secret in the config file."
    ))?;
    let (tx_exit, rx_exit) = watch::channel(false);
    let metrics_registry = Arc::new(RwLock::new(Registry::default()));
    let rpc_metrics = Arc::new(RpcMetrics::new(metrics_registry.clone()).await);

    let keeper_metrics: Arc<KeeperMetrics> =
        Arc::new(KeeperMetrics::new(metrics_registry.clone()).await);

    let keeper_private_key_option = config.keeper.private_key.load()?;
    if keeper_private_key_option.is_none() {
        tracing::info!("Not starting keeper service: no keeper private key specified. Please add one to the config if you would like to run the keeper service.")
    }
```
