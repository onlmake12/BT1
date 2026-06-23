[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** util/types/src/core/hardfork/ckb2021.rs (L27-30)
```rust
    /// Use input cell committing block timestamp as the start time for the relative timestamp in `since`.
    ///
    /// Ref: [CKB RFC 0028](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0028-change-since-relative-timestamp/0028-change-since-relative-timestamp.md)
    pub rfc_0028: Option<EpochNumber>,
```

**File:** util/types/src/core/hardfork/ckb2021.rs (L87-99)
```rust
    pub fn new_mirana() -> Self {
        // Use a builder to ensure all features are set manually.
        Self::new_builder()
            .rfc_0028(hardfork::mainnet::RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH)
            .rfc_0029(hardfork::mainnet::CKB2021_START_EPOCH)
            .rfc_0030(hardfork::mainnet::CKB2021_START_EPOCH)
            .rfc_0031(hardfork::mainnet::CKB2021_START_EPOCH)
            .rfc_0032(hardfork::mainnet::RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH)
            .rfc_0036(hardfork::mainnet::CKB2021_START_EPOCH)
            .rfc_0038(hardfork::mainnet::CKB2021_START_EPOCH)
            .build()
            .unwrap()
    }
```

**File:** util/types/src/core/hardfork/ckb2021.rs (L129-136)
```rust
define_methods!(
    CKB2021,
    rfc_0028,
    block_ts_as_relative_since_start,
    is_block_ts_as_relative_since_start_enabled,
    disable_rfc_0028,
    "RFC PR 0028"
);
```

**File:** util/constant/src/hardfork/mainnet.rs (L5-5)
```rust
pub const RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH: u64 = 5414;
```
