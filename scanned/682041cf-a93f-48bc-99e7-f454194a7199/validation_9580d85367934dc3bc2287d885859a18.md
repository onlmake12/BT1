### Title
Hard-Coded Weak Default Database Password in Production Source — (File: `util/app-config/src/configs/rich_indexer.rs`)

### Summary
The CKB rich-indexer's PostgreSQL configuration defaults to the hard-coded password `"123456"` in production Rust source code. Any node operator who enables the PostgreSQL-backed rich-indexer without explicitly overriding `db_password` will silently deploy with this trivially guessable, publicly known credential, exposing the indexer database to unauthorized access.

### Finding Description
In `util/app-config/src/configs/rich_indexer.rs`, the `default_db_password()` function returns the literal string `"123456"`:

```rust
fn default_db_password() -> String {
    "123456".to_string()
}
```

This function is registered as the serde default for the `db_password` field of `RichIndexerConfig`:

```rust
#[serde(default = "default_db_password")]
pub db_password: String,
```

And it is also used directly in the `Default` impl:

```rust
db_password: default_db_password(),
```

The companion template file `resource/ckb.toml` reinforces this weak credential by showing `db_password = "123456"` as the example value in the commented-out PostgreSQL block (lines 296–304), making it likely that operators who uncomment the block will leave the password unchanged. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
An attacker who can reach the PostgreSQL port used by the rich-indexer can authenticate with the well-known default password `"123456"` and:

1. **Read** all indexed cell/transaction data (information disclosure).
2. **Modify or delete** indexed rows, causing RPC methods backed by the rich-indexer (`get_cells`, `get_transactions`, `get_cells_capacity`, etc.) to return fabricated or empty results to any RPC caller.
3. **Deny service** to all consumers of the rich-indexer RPC module by truncating tables.

The RPC caller is an explicitly in-scope attacker role. Corrupted indexer responses can mislead wallets, dApps, and block explorers that rely on the CKB rich-indexer RPC. [4](#0-3) 

### Likelihood Explanation
- The password `"123456"` is committed in plain text in a public repository, making it universally known to any reader of the source.
- The `resource/ckb.toml` template actively shows this value as the example, nudging operators to use it without modification.
- PostgreSQL is commonly configured to listen on `0.0.0.0` in cloud/server deployments, making the port reachable from the network.
- No warning, validation, or forced-change prompt exists in the CKB startup path to alert the operator that the default password is in use. [5](#0-4) 

### Recommendation
1. Remove the hard-coded `"123456"` default. Replace `default_db_password()` with a function that returns an empty string and require the operator to supply an explicit password when `db_type = "postgres"` is configured.
2. Add a startup validation check: if `db_type == Postgres` and `db_password` is empty or equals the known-weak default, emit a fatal error or at minimum a prominent warning and refuse to start.
3. Update `resource/ckb.toml` to replace `db_password = "123456"` with `db_password = "<CHANGE_ME>"` or remove the example value entirely.
4. Document in the rich-indexer README that operators must set a strong, unique password before exposing the PostgreSQL port. [6](#0-5) 

### Proof of Concept

1. Enable the rich-indexer with PostgreSQL in `ckb.toml` by uncommenting the block shown at lines 296–304 of `resource/ckb.toml`, leaving `db_password = "123456"` as-is (or simply omitting `db_password` entirely, since the Rust default is identical).
2. Start the CKB node. `RichIndexerConfig::default()` supplies `"123456"` as the PostgreSQL password via `default_db_password()`.
3. From any host that can reach the PostgreSQL port, run:
   ```
   psql -h <node_host> -p 8532 -U postgres -d ckb-rich-indexer
   Password: 123456
   ```
4. Authentication succeeds. The attacker now has full read/write access to the rich-indexer database.
5. Execute `DELETE FROM output;` to empty the cell table. Subsequent `get_cells` RPC calls return empty results, breaking any application relying on the indexer. [1](#0-0) [7](#0-6)

### Citations

**File:** util/app-config/src/configs/rich_indexer.rs (L49-51)
```rust
    /// The database password.
    #[serde(default = "default_db_password")]
    pub db_password: String,
```

**File:** util/app-config/src/configs/rich_indexer.rs (L54-65)
```rust
impl Default for RichIndexerConfig {
    fn default() -> Self {
        Self {
            db_type: DBDriver::default(),
            store: PathBuf::default(),
            db_name: default_db_name(),
            db_host: default_db_host(),
            db_port: default_db_port(),
            db_user: default_db_user(),
            db_password: default_db_password(),
        }
    }
```

**File:** util/app-config/src/configs/rich_indexer.rs (L68-86)
```rust
fn default_db_name() -> String {
    "ckb-rich-indexer".to_string()
}

fn default_db_host() -> String {
    "127.0.0.1".to_string()
}

fn default_db_port() -> u16 {
    8532
}

fn default_db_user() -> String {
    "postgres".to_string()
}

fn default_db_password() -> String {
    "123456".to_string()
}
```

**File:** resource/ckb.toml (L296-304)
```text
# [indexer_v2.rich_indexer]
# # By default, it uses an embedded SQLite database.
# # Alternatively, you can set up a PostgreSQL database service and provide the connection parameters.
# db_type = "postgres"
# db_name = "ckb-rich-indexer"
# db_host = "127.0.0.1"
# db_port = 5432
# db_user = "postgres"
# db_password = "123456"
```
