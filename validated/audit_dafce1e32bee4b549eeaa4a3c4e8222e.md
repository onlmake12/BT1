### Title
Sensitive Credentials (`tor_password`, `proxy_url`) Exposed via Unredacted `Debug` + `Serialize` Derivation on Network Config Structs — (File: `util/app-config/src/configs/network.rs`)

### Summary
`OnionConfig` and `ProxyConfig` both derive `Debug` and `Serialize` while containing sensitive credentials — the Tor controller plaintext password (`tor_password`) and a SOCKS5 proxy URL that embeds username/password (`proxy_url`). The parent `Config` struct also derives `Debug` and `Serialize`. Any code path that formats these structs with `{:?}` (e.g., in error messages, panic output, or debug log lines) will emit the raw credentials into the node's log files, which are a shared, persistent medium accessible to other local users or processes — a direct analog to clipboard leakage.

### Finding Description
In `util/app-config/src/configs/network.rs`:

- `Config` (line 20) derives `#[derive(Clone, Debug, Serialize, Deserialize, Default)]` and contains both `proxy: ProxyConfig` and `onion: OnionConfig` as public fields.
- `ProxyConfig` (line 117) derives the same set and contains `proxy_url: Option<String>`, documented inline as `// like: socks5://username:password@127.0.0.1:1080`.
- `OnionConfig` (line 132) derives the same set and contains `tor_password: Option<String>`, the plaintext Tor ControlPort password.

Neither struct implements a custom `Debug` that redacts sensitive fields, nor wraps the sensitive values in a type that suppresses their output (e.g., `secrecy::Secret<String>`). Because `Config` itself derives `Debug`, any `format!("{:?}", config.network)` or `format!("{:?}", config.network.onion)` call — whether in an error path, a panic handler, or a debug log — will print the raw password and proxy credentials verbatim.

The `Serialize` derivation compounds this: if any future or existing code serializes the network config to JSON (e.g., for diagnostics, RPC introspection, or config-dump tooling), the credentials appear in the serialized output without any masking.

The `authenticate` function in `util/onion/src/tor_controller.rs` (line 128) receives `tor_password: Option<String>` and, on the unsupported-auth-method error path (line 186–191), formats `proto_info` with `{:?}`. While `proto_info` itself does not contain the password, the broader pattern of `{:?}`-formatting structs in error paths throughout the codebase creates a realistic accidental-exposure surface for the parent config structs that do contain the password.

### Impact Explanation
A node operator who configures `[network.onion] tor_password` or `[network.proxy] proxy_url` with embedded credentials is exposed to credential leakage into the node's log files. Log files on a shared server (multi-user Linux host, container with mounted log volume, centralized log aggregation) are readable by other local users or by log-shipping infrastructure. An attacker with read access to the logs obtains:
- The Tor ControlPort password, enabling them to issue arbitrary Tor control commands (circuit manipulation, hidden-service teardown, deanonymization of the node's onion address).
- The SOCKS5 proxy credentials, enabling proxy account takeover or traffic correlation.

This is structurally identical to the clipboard finding: a sensitive secret is placed into a shared, persistent medium (logs vs. clipboard) accessible to co-resident processes or users.

### Likelihood Explanation
- Tor/onion and proxy support are production features documented in `resource/ckb.toml` (lines 141–174), not experimental.
- Operators who enable these features and set passwords are the affected population.
- Rust's `{:?}` formatting is the default way developers inspect structs in error messages and log lines; accidental inclusion is a routine occurrence in large codebases.
- Log files are routinely shipped to external aggregators (Datadog, ELK, etc.), widening the exposure surface beyond the local host.

Impact: 3 | Likelihood: 2

### Recommendation
1. Implement a custom `Debug` for `OnionConfig` and `ProxyConfig` that replaces sensitive fields with `"[REDACTED]"`.
2. Alternatively, wrap `tor_password` and `proxy_url` in `secrecy::Secret<String>`, which suppresses `Debug` output by default.
3. Audit all `format!("{:?}", …)` and `log::*!("{:?}", …)` call sites that could transitively reach `Config`, `OnionConfig`, or `ProxyConfig`.
4. Consider adding a `#[serde(skip_serializing)]` or custom `Serialize` that omits these fields from any JSON output.

### Proof of Concept
```toml
# ckb.toml
[network.onion]
listen_on_onion = true
tor_controller   = "127.0.0.1:9051"
tor_password     = "s3cr3tTorPass"

[network.proxy]
proxy_url = "socks5://proxyuser:proxypass@127.0.0.1:1080"
```

Any code path that executes:
```rust
// e.g., in an error handler or debug log
error!("Network config: {:?}", ckb_app_config.network);
// or
format!("{:?}", ckb_app_config.network.onion)
```
will emit to the log file:
```
OnionConfig { ..., tor_password: Some("s3cr3tTorPass"), ... }
ProxyConfig { proxy_url: Some("socks5://proxyuser:proxypass@127.0.0.1:1080"), ... }
```

The log file is a shared, persistent artifact — readable by co-resident users, log-shipping agents, or any process with filesystem access — directly analogous to clipboard data being read by co-resident applications. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** util/app-config/src/configs/network.rs (L20-22)
```rust
#[derive(Clone, Debug, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct Config {
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

**File:** util/onion/src/tor_controller.rs (L128-192)
```rust
pub async fn authenticate(
    tor_password: Option<String>,
    utc: &mut UnauthenticatedConn<TcpStream>,
) -> Result<(), Error> {
    let proto_info = utc.load_protocol_info().await.map_err(|err| {
        InternalErrorKind::Other.other(format!("Failed to load protocol info: {:?}", err))
    })?;
    proto_info.auth_methods.iter().for_each(|m| {
        info!("Tor Server Controller supports auth method: {:?}", m);
    });
    if proto_info.auth_methods.contains(&TorAuthMethod::Null) {
        utc.authenticate(&TorAuthData::Null).await.map_err(|err| {
            InternalErrorKind::Other.other(format!("Failed to authenticate with null: {:?}", err))
        })?;
        if tor_password.is_some() {
            warn!("Password not required for the Tor controller, but `tor_password` is configured in [network.onion].");
        }
        return Ok(());
    }

    if proto_info
        .auth_methods
        .contains(&TorAuthMethod::HashedPassword)
    {
        match tor_password {
            Some(tor_password) => {
                utc.authenticate(&TorAuthData::HashedPassword(Cow::Owned(tor_password)))
                    .await
                    .map_err(|err| {
                        InternalErrorKind::Other
                            .other(format!("Failed to authenticate with password: {:?}", err))
                    })?;
                return Ok(());
            }
            None => {
                warn!("Tor server requires a password, but none is configured");
            }
        }
    }

    if proto_info.auth_methods.contains(&TorAuthMethod::Cookie)
        || proto_info.auth_methods.contains(&TorAuthMethod::SafeCookie)
    {
        let cookie = load_auth_cookie(proto_info).await?;
        let tor_auth_data = {
            if proto_info.auth_methods.contains(&TorAuthMethod::Cookie) {
                debug!("Using Cookie auth method...");
                TorAuthData::Cookie(Cow::Owned(cookie))
            } else {
                debug!("Using SafeCookie auth method...");
                TorAuthData::SafeCookie(Cow::Owned(cookie))
            }
        };
        utc.authenticate(&tor_auth_data).await.map_err(|err| {
            InternalErrorKind::Other.other(format!("Failed to authenticate with cookie: {:?}", err))
        })?;
        return Ok(());
    }
    Err(InternalErrorKind::Other
        .other(format!(
            "Tor server does not support any authentication method; proto_info: {:?}",
            proto_info
        ))
        .into())
}
```
