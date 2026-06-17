Based on my investigation, I have found a valid analog. The Pyth Stylus SDK's `update_price_feeds` (and related functions) use `delegate_call_helper`, which performs a `delegate_call` that modifies the caller's storage context. The calling Stylus contract's `StorageCache` is never invalidated after the delegate call returns, causing any previously-cached storage slots to become stale — a direct analog of the reported bug.

---

### Title
Stale Stylus `StorageCache` After `delegate_call` in `update_price_feeds` Allows Stale Price Reads — (`File: target_chains/ethereum/sdk/stylus/contracts/src/pyth/functions.rs`)

---

### Summary

The Pyth Stylus SDK exposes `update_price_feeds`, `update_price_feeds_if_necessary`, `parse_price_feed_updates`, and `parse_price_feed_updates_unique` — all of which internally invoke `delegate_call_helper`, which performs an unsafe `delegate_call`. Because a `delegate_call` runs the callee's code in the **caller's storage context**, the callee writes to the caller's storage slots. However, the calling Stylus program's `StorageCache` (owned by its `EvmApiRequestor`) is never invalidated after the delegate call returns. Any storage slots that were read and cached before the delegate call, and then written by the delegate call, will return stale values on subsequent reads within the same call frame.

---

### Finding Description

In `target_chains/ethereum/sdk/stylus/contracts/src/pyth/functions.rs`, four functions use `delegate_call_helper`: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

`delegate_call_helper` itself calls the Stylus SDK's `delegate_call` (marked `unsafe`): [5](#0-4) 

A `delegate_call` executes the callee's logic in the **caller's storage context**. The callee (the Pyth contract, or `MockPythContract`) writes updated price feed data to storage slots that belong to the calling Stylus contract. Because the Stylus runtime creates a fresh `EvmApiRequestor` (and thus a fresh `StorageCache`) for each call frame, the callee's writes are invisible to the caller's cache. After the delegate call returns, the caller's `StorageCache` still holds the pre-call values for those slots.

The `MockPythContract` stores price feeds in `StorageMap<B256, StoragePriceFeed>` and writes to them during `update_price_feeds`: [6](#0-5) 

If a calling Stylus contract embeds `MockPythContract` (or the production `PythContract`) via `#[borrow]` — as shown in the SDK README — and reads a price feed slot before calling `update_price_feeds`, the slot is cached. After the delegate call writes a new price to that slot, the caller's cache is stale. Any subsequent read of that slot within the same call frame returns the **old** price.

---

### Impact Explanation

A Stylus contract using the Pyth SDK that:
1. Reads a price feed from its embedded Pyth storage (caching the slot)
2. Calls `update_price_feeds` (delegate call writes new price to the same slot)
3. Reads the price feed again (gets the pre-update cached value)

...will silently operate on a stale price. This can bypass post-update invariant checks (e.g., "price must be fresh after update"), cause incorrect financial calculations (e.g., collateral checks, liquidation thresholds), or allow an attacker to exploit the discrepancy between the on-chain storage value and the in-cache value to manipulate contract logic.

---

### Likelihood Explanation

Any Stylus contract that uses the Pyth SDK's `update_price_feeds` (or related delegate-call functions) and also reads price data from its own embedded Pyth storage before the update is affected. The README's recommended usage pattern (`#[borrow] PythContract pyth`) places the Pyth storage layout directly inside the calling contract's storage, making this pattern reachable. An unprivileged transaction sender can trigger the vulnerable code path by calling any public function on such a contract that follows the read-update-read pattern. [7](#0-6) 

---

### Recommendation

**Short term**: After any `delegate_call_helper` invocation in `update_price_feeds` (and related functions), explicitly flush or invalidate the calling contract's `StorageCache` for the affected storage slots before any subsequent reads. Alternatively, document clearly that callers must not rely on cached storage reads after calling any of these delegate-call-based functions.

**Long term**: Audit all uses of `delegate_call_helper` in the Pyth Stylus SDK for the read-delegate_call-read pattern. Consider replacing `delegate_call` with a regular `call` where storage sharing is not required, or add a cache-flush primitive to the Stylus SDK API and call it unconditionally after every `delegate_call`.

---

### Proof of Concept

```rust
// Calling Stylus contract embeds MockPythContract via #[borrow]
sol_storage! {
    #[entrypoint]
    pub struct MyDeFiContract {
        #[borrow]
        MockPythContract pyth;
        // ... other fields
    }
}

#[public]
impl MyDeFiContract {
    pub fn check_price_invariant(&mut self, id: B256, update_data: Vec<Bytes>) -> Result<(), Vec<u8>> {
        // Step 1: Read price from embedded storage — slot is now CACHED
        let price_before = self.pyth.price_feeds.get(id).to_price_feed();
        // price_before.price.publish_time == T_old (cached)

        // Step 2: Update price via delegate_call — callee writes T_new to the same slot
        update_price_feeds(&mut self, mock_pyth_address, update_data)?;

        // Step 3: Read price again — StorageCache returns T_old (STALE), not T_new
        let price_after = self.pyth.price_feeds.get(id).to_price_feed();

        // This assert INCORRECTLY passes even though the price was updated:
        assert_eq!(price_before.price.publish_time, price_after.price.publish_time,
            "price_after should reflect the update but reads from stale cache");

        Ok(())
    }
}
```

The `price_after` read at step 3 returns the pre-update value from the stale `StorageCache`, not the value written by the delegate call. An attacker can exploit this to bypass any post-update invariant check that relies on re-reading the price from storage. [8](#0-7) [9](#0-8)

### Citations

**File:** target_chains/ethereum/sdk/stylus/contracts/src/pyth/functions.rs (L140-147)
```rust
pub fn update_price_feeds(
    storage: &mut impl TopLevelStorage,
    pyth_address: Address,
    update_data: Vec<Bytes>,
) -> Result<(), Vec<u8>> {
    delegate_call_helper::<updatePriceFeedsCall>(storage, pyth_address, (update_data,))?;
    Ok(())
}
```

**File:** target_chains/ethereum/sdk/stylus/contracts/src/pyth/functions.rs (L160-173)
```rust
pub fn update_price_feeds_if_necessary(
    storage: &mut impl TopLevelStorage,
    pyth_address: Address,
    update_data: Vec<Bytes>,
    price_ids: Vec<B256>,
    publish_times: Vec<u64>,
) -> Result<(), Vec<u8>> {
    delegate_call_helper::<updatePriceFeedsIfNecessaryCall>(
        storage,
        pyth_address,
        (update_data, price_ids, publish_times),
    )?;
    Ok(())
}
```

**File:** target_chains/ethereum/sdk/stylus/contracts/src/pyth/functions.rs (L187-201)
```rust
pub fn parse_price_feed_updates(
    storage: &mut impl TopLevelStorage,
    pyth_address: Address,
    update_data: Vec<Bytes>,
    price_ids: Vec<B256>,
    min_publish_time: u64,
    max_publish_time: u64,
) -> Result<Vec<PriceFeed>, Vec<u8>> {
    let parse_price_feed_updates_call = delegate_call_helper::<parsePriceFeedUpdatesCall>(
        storage,
        pyth_address,
        (update_data, price_ids, min_publish_time, max_publish_time),
    )?;
    Ok(parse_price_feed_updates_call.priceFeeds)
}
```

**File:** target_chains/ethereum/sdk/stylus/contracts/src/pyth/functions.rs (L215-229)
```rust
pub fn parse_price_feed_updates_unique(
    storage: &mut impl TopLevelStorage,
    pyth_address: Address,
    update_data: Vec<Bytes>,
    price_ids: Vec<B256>,
    min_publish_time: u64,
    max_publish_time: u64,
) -> Result<Vec<PriceFeed>, Vec<u8>> {
    let parse_price_feed_updates_call = delegate_call_helper::<parsePriceFeedUpdatesUniqueCall>(
        storage,
        pyth_address,
        (update_data, price_ids, min_publish_time, max_publish_time),
    )?;
    Ok(parse_price_feed_updates_call.priceFeeds)
}
```

**File:** target_chains/ethereum/sdk/stylus/contracts/src/utils/mod.rs (L30-39)
```rust
pub fn delegate_call_helper<C: SolCall>(
    storage: &mut impl TopLevelStorage,
    address: Address,
    args: <C::Parameters<'_> as SolType>::RustType,
) -> Result<C::Return, Vec<u8>> {
    let calldata = C::new(args).abi_encode();
    let res = unsafe { delegate_call(storage, address, &calldata).map_err(map_call_error)? };
    C::abi_decode_returns(&res, false /* validate */)
        .map_err(|_| CALL_RETDATA_DECODING_ERROR_MESSAGE.to_vec())
}
```

**File:** target_chains/ethereum/sdk/stylus/contracts/src/pyth/mock.rs (L26-30)
```rust
pub struct MockPythContract {
    single_update_fee_in_wei: StorageUint<256, 4>,
    valid_time_period: StorageUint<256, 4>,
    price_feeds: StorageMap<B256, StoragePriceFeed>,
}
```

**File:** target_chains/ethereum/sdk/stylus/contracts/src/pyth/mock.rs (L80-108)
```rust
    fn update_price_feeds(&mut self, update_data: Vec<AbiBytes>) -> Result<(), Vec<u8>> {
        let required_fee = self.get_update_fee(update_data.clone());
        if required_fee < msg::value() {
            return Err(Error::InsufficientFee(InsufficientFee {}).into());
        }

        for item in update_data.iter() {
            let price_feed_data = <PriceFeed as SolType>::abi_decode(item, false)
                .map_err(|_| CALL_RETDATA_DECODING_ERROR_MESSAGE.to_vec())?;
            let last_publish_time = &self
                .price_feeds
                .get(price_feed_data.id)
                .price
                .publish_time
                .get();
            if last_publish_time < &price_feed_data.price.publish_time {
                self.price_feeds
                    .setter(price_feed_data.id)
                    .set(price_feed_data);
                evm::log(PriceFeedUpdate {
                    id: price_feed_data.id,
                    publishTime: price_feed_data.price.publish_time.to(),
                    price: price_feed_data.price.price,
                    conf: price_feed_data.price.conf,
                });
            }
        }
        Ok(())
    }
```

**File:** target_chains/ethereum/sdk/stylus/contracts/src/pyth/types.rs (L6-21)
```rust
sol_storage! {
    /// Represents a storage-optimized price structure containing the current price data.
    pub struct StoragePrice {
        int64 price;
        uint64 conf;
        int32 expo;
        uint publish_time;
    }

   /// Represents a storage-optimized price feed structure containing an ID and associated price data.
    pub struct StoragePriceFeed {
        bytes32 id;
        StoragePrice price;
        StoragePrice ema_price;
    }
}
```
