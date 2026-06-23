### Title
Cleartext Storage of PostgreSQL Database Password in Rich Indexer Config File - (File: `util/app-config/src/configs/rich_indexer.rs`)

### Summary
The `RichIndexerConfig` struct stores the PostgreSQL database password (`db_password`) as a plaintext `String` field, serialized directly into the node's TOML configuration file without any encryption or encoding. The hardcoded default value is `"123456"`. Any local user or process with read access to `ckb.toml` can trivially retrieve the database credential and use it to authenticate directly to the PostgreSQL backend.

### Finding Description
`RichIndexerConfig` in `util/app-config/src/configs/rich_indexer.rs` declares `db_password` as a plain `String` with a weak default:

```rust
/// The database password.
#[serde(default = "default_db_password")]
pub db_password: String,
```

```rust
fn default_db_password() -> String {
    "123456".to_string()
}
``` [1](#0-0) [2](#0-1) 

Because `RichIndexerConfig` derives `Serialize`/`Deserialize`, the password is written and read as plaintext in `ckb.toml`. The official README even documents the default credential in a code block:

```toml
db_password = "123456"
``` [3](#0-2) 

The password is then consumed verbatim to build the PostgreSQL connection URL in `util/rich-indexer/src/store.rs`: [4](#0-3) 

### Impact Explanation
An attacker or unprivileged local user who can read `ckb.toml` (e.g., via a misconfigured file permission, a path-traversal bug in another service, or shared hosting) immediately obtains the PostgreSQL credential. With that credential they can connect directly to the rich-indexer database, read all indexed chain data, or corrupt the index — causing the node to serve incorrect query results to RPC callers without any on-chain evidence of tampering.

### Likelihood Explanation
The default password `"123456"` is trivially guessable even without reading the file. Operators who follow the documented example and do not change the default are exposed by default. The rich indexer is an opt-in feature (`--rich-indexer`), but its documentation actively encourages PostgreSQL use for production secondary development, making real-world deployment plausible.

### Recommendation
- Do not store database credentials in the TOML config file in plaintext. Support reading the password from an environment variable (e.g., `CKB_RICH_INDEXER_DB_PASSWORD`) or a separate secrets file with restricted permissions.
- Remove the hardcoded default `"123456"` and require the operator to supply a password explicitly when `db_type = "postgres"` is selected.
- Add a startup warning if the password equals the known-weak default.
- Document the recommended file permission (`0600`) for `ckb.toml`.

### Proof of Concept
1. Deploy a CKB node with `--rich-indexer` and `db_type = "postgres"` using the documented defaults.
2. Read `ckb.toml` (world-readable by default on many Linux installs):
   ```
   cat /path/to/ckb/ckb.toml | grep db_password
   # db_password = "123456"
   ```
3. Connect directly to PostgreSQL with the recovered credential:
   ```
   psql -h 127.0.0.1 -p 8532 -U postgres -d ckb-rich-indexer
   # Password: 123456
   ```
4. The attacker now has full read/write access to the rich-indexer database, enabling silent corruption of indexed data served to RPC callers.

### Citations

**File:** util/app-config/src/configs/rich_indexer.rs (L49-51)
```rust
    /// The database password.
    #[serde(default = "default_db_password")]
    pub db_password: String,
```

**File:** util/app-config/src/configs/rich_indexer.rs (L84-86)
```rust
fn default_db_password() -> String {
    "123456".to_string()
}
```

**File:** util/rich-indexer/README.md (L91-102)
```markdown
```toml
# CKB rich-indexer has its unique configuration.
[indexer_v2.rich_indexer]
# By default, it uses an embedded SQLite database.
# Alternatively, you can set up a PostgreSQL database service and provide the connection parameters.
db_type = "postgres"
db_name = "ckb-rich-indexer"
db_host = "127.0.0.1"
db_port = 5432
db_user = "postgres"
db_password = "123456"
```
```

**File:** util/rich-indexer/src/store.rs (L72-80)
```rust
            DBDriver::Postgres => {
                self.postgres_init(db_config).await?;
                let uri = build_url_for_postgres(db_config);
                let connection_options =
                    AnyConnectOptions::from_str(&uri)?.log_statements(LevelFilter::Trace);
                let pool = pool_options.connect_with(connection_options).await?;
                log::info!("PostgreSQL is connected.");
                self.pool
                    .set(pool.clone())
```
