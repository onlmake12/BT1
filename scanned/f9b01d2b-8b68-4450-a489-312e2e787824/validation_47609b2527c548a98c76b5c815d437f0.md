### Title
SOCKS5 Proxy Credentials Logged Unconditionally at INFO Level — (File: network/src/network.rs)

### Summary
When a CKB node is configured with a SOCKS5 proxy that includes authentication credentials, the full proxy URL (including embedded `username:password`) is logged unconditionally at the `info!` level on every startup. This is not gated behind any debug flag and is always written to the log file and stdout.

### Finding Description

`ProxyConfig.proxy_url` is documented to accept URLs of the form `socks5://username:password@127.0.0.1:1080`: [1](#0-0) 

During network service initialization, the full proxy URL — including any embedded credentials — is passed directly to `info!()` with no sanitization or redaction: [2](#0-1) 

The `info!` macro in CKB is always active and unconditionally writes to the log file and stdout: [3](#0-2) 

The logger service writes every matched record to file and stdout with no credential-stripping logic: [4](#0-3) 

### Impact Explanation

Any node operator who configures a SOCKS5 proxy with credentials (the documented use case) will have those credentials written in plaintext to:
- The CKB log file on disk
- Standard output (which may be captured by process supervisors, systemd journal, or container log drivers)
- Any log aggregation or crash-reporting service (e.g., sentry, which is explicitly integrated in CKB) [5](#0-4) 

An attacker with read access to the log files, log aggregation backend, or sentry breadcrumbs can recover the SOCKS5 proxy credentials and use them to abuse or impersonate the proxy service.

### Likelihood Explanation

This is a deterministic, unconditional exposure — not probabilistic. Every node that configures a SOCKS5 proxy with credentials (the documented and expected format) will have those credentials logged on every startup. No special attacker action is required beyond gaining access to the log output, which is a common attack surface (log aggregation services, shared hosting, container orchestration, etc.).

### Recommendation

Strip credentials from the proxy URL before logging. Use a helper that replaces the userinfo component with a redacted placeholder:

```rust
fn redact_proxy_url(url: &str) -> String {
    if let Ok(mut parsed) = url::Url::parse(url) {
        if parsed.password().is_some() {
            let _ = parsed.set_password(Some("***"));
        }
        if !parsed.username().is_empty() {
            let _ = parsed.set_username("***");
        }
        parsed.to_string()
    } else {
        "<unparseable proxy url>".to_string()
    }
}
```

Then replace the logging call:

```rust
info!(
    "set tcp_proxy_config: {:?}, proxy_random_auth: {}",
    config.proxy.proxy_url.as_deref().map(redact_proxy_url),
    config.proxy.proxy_random_auth
);
```

### Proof of Concept

1. Configure `ckb.toml` with:
   ```toml
   [network.proxy]
   proxy_url = "socks5://myuser:mysecretpassword@127.0.0.1:1080"
   ```
2. Start the CKB node.
3. Observe in the log file or stdout:
   ```
   INFO  set tcp_proxy_config: Some("socks5://myuser:mysecretpassword@127.0.0.1:1080"), proxy_random_auth: true
   ```

The credential `mysecretpassword` is written unconditionally to the log on every startup, with no debug flag required. [6](#0-5) [2](#0-1)

### Citations

**File:** util/app-config/src/configs/network.rs (L116-124)
```rust
/// Proxy related config options
#[derive(Clone, Debug, Serialize, Deserialize, Default)]
pub struct ProxyConfig {
    // like: socks5://username:password@127.0.0.1:1080
    pub proxy_url: Option<String>,
    // use random auth for each proxy connection
    #[serde(default = "default_proxy_random_auth")]
    pub proxy_random_auth: bool,
}
```

**File:** network/src/network.rs (L989-998)
```rust
            if let Some(proxy_url) = &config.proxy.proxy_url {
                service_builder = service_builder
                    .tcp_proxy_config(proxy_url)
                    .tcp_proxy_random_auth(config.proxy.proxy_random_auth);
                info!(
                    "set tcp_proxy_config: {:?}, proxy_random_auth: {}",
                    config.proxy.proxy_url.clone(),
                    config.proxy.proxy_random_auth
                );
            };
```

**File:** util/logger/src/lib.rs (L140-144)
```rust
#[macro_export(local_inner_macros)]
macro_rules! error {
    ($( $args:tt )*) => {
        $crate::internal::error!($( $args )*);
    }
```

**File:** util/logger-service/src/lib.rs (L407-462)
```rust
    fn log(&self, record: &Record) {
        // Check if the record is matched by the main filter
        let is_match = self.filter.read().matches(record);
        let extras = self
            .extra_loggers
            .read()
            .iter()
            .filter_map(|(name, logger)| {
                if logger.filter.matches(record) {
                    Some(name.to_owned())
                } else {
                    None
                }
            })
            .collect::<Vec<_>>();
        if is_match || !extras.is_empty() {
            #[cfg(feature = "with_sentry")]
            if self.emit_sentry_breadcrumbs {
                use sentry::{add_breadcrumb, integrations::log::breadcrumb_from_record};
                add_breadcrumb(|| breadcrumb_from_record(record));
            }

            let thread = thread::current();
            let thread_name = thread.name().unwrap_or("*unnamed*");

            let utc = OffsetDateTime::now_utc();
            let fmt = FORMAT.get_or_init(|| {
                format_description::parse(
                    "[year]-[month]-[day] [hour]:[minute]:[second].[subsecond digits:3] \
                    [offset_hour sign:mandatory]:[offset_minute]",
                )
                .expect("DateTime format_description")
            });
            if let Ok(dt) = utc.format(&fmt) {
                let with_color = {
                    let thread_name = format!("{}", Paint::blue(thread_name).bold());
                    let date = format!("{}", Paint::rgb(47, 79, 79, &dt).bold()); // darkslategrey
                    format!(
                        "{} {} {} {}  {}",
                        date,
                        thread_name,
                        record.level(),
                        record.target(),
                        record.args()
                    )
                };
                let _ = self.sender.send(Message::Record {
                    is_match,
                    extras,
                    data: with_color,
                    level: record.level(),
                    target: record.target().to_string(),
                    date: dt,
                    original_message: format!("{}", record.args()),
                });
            }
```

**File:** ckb-bin/src/setup_guard.rs (L44-54)
```rust
        let sentry_guard = if setup.is_sentry_enabled {
            let sentry_config = setup.config.sentry();

            ckb_logger::info_target!(
                crate::LOG_TARGET_SENTRY,
                "**Notice**: \
                 The ckb process will send stack trace to sentry on Rust panics. \
                 This is enabled by default before mainnet, which can be opted out by setting \
                 the option `dsn` to empty in the config file. The DSN is now {}",
                sentry_config.dsn
            );
```
