Looking at the exact code flow in `update_single_price_feed` and `is_fresh_update`:

The code path is fully traceable and the claim is accurate. Here is the analysis:

---

### Title
Fee Charged Without Price Update When `publish_time` Equals Cached Timestamp — (`target_chains/sui/contracts/sources/pyth.move`)

### Summary
`update_single_price_feed` unconditionally deposits the caller's fee coin into the `PriceInfoObject` before performing the freshness check. Because `is_fresh_update` uses strict greater-than (`update_timestamp > cached_timestamp`), a submission whose `publish_time` exactly equals the cached timestamp passes all validation, consumes the fee, and silently skips the price write.

### Finding Description

In `update_single_price_feed`, the fee is deposited at line 277 before `update_cache` is ever called: [1](#0-0) 

`update_cache` then delegates the freshness decision to `is_fresh_update`: [2](#0-1) 

`is_fresh_update` uses strict `>`: [3](#0-2) 

When `update_timestamp == cached_timestamp`, `is_fresh_update` returns `false`, `update_price_info_object` is never called, but `deposit_fee_coins` has already irrevocably merged the caller's coin into the `PriceInfoObject`'s dynamic field: [4](#0-3) 

There is no refund path and no abort — the function returns normally, giving the caller no signal that no update occurred.

### Impact Explanation

Any caller of `update_single_price_feed` who submits a cryptographically valid accumulator update whose `publish_time` matches the currently cached value loses `base_update_fee` SUI with no corresponding price write. This occurs naturally in race conditions (two relayers submit the same signed accumulator message in the same block) or on any retry of a previously-applied update. The lost funds accumulate in the `PriceInfoObject` with no on-chain withdrawal mechanism visible in the contract.

The "oracle price freeze" sub-claim in the question is **not independently achievable**: a higher-timestamp update submitted by any other party will still be applied normally; the equal-timestamp submission does not block it.

### Likelihood Explanation

The scenario is reachable by any unprivileged caller without any privileged access. Race conditions between competing relayers are a normal operational occurrence on Sui (parallel transaction execution). A relayer that retries a failed transaction with the same accumulator message will also trigger this path. No special setup beyond a valid, Wormhole-verified accumulator message is required.

### Recommendation

Move `deposit_fee_coins` to execute **only when `is_fresh_update` returns `true`**, or alternatively abort/refund the fee when no update is applied. The simplest fix is to restructure `update_single_price_feed` so the fee deposit is conditional on the result of `update_cache` (which should return a boolean indicating whether the write occurred).

### Proof of Concept

1. Deploy with `DEFAULT_BASE_UPDATE_FEE = 50`.
2. Call `update_single_price_feed` with a valid accumulator message containing `publish_time = T`. Price is written; fee balance in `PriceInfoObject` = 50.
3. Call `update_single_price_feed` again with the **same** accumulator message (`publish_time = T`). `is_fresh_update` returns `false`; price unchanged; fee balance in `PriceInfoObject` = 100.
4. Assert: price feed timestamp is still `T` (no update), but the caller has paid 100 total SUI for one effective update.

The existing test `test_create_and_update_price_feeds_with_batch_attestation_success` already asserts `price_info::get_balance(&price_info_object_1)==DEFAULT_BASE_UPDATE_FEE` after one call; a second identical call would show the balance doubling while the price stays the same, confirming the invariant violation. [5](#0-4)

### Citations

**File:** target_chains/sui/contracts/sources/pyth.move (L274-277)
```text
        assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);

        // store fee coins within price info object
        price_info::deposit_fee_coins(price_info_object, fee);
```

**File:** target_chains/sui/contracts/sources/pyth.move (L316-322)
```text
        if (is_fresh_update(update, price_info_object)){
            pyth_event::emit_price_feed_update(price_feed::from(price_info::get_price_feed(update)), clock::timestamp_ms(clock)/1000);
            price_info::update_price_info_object(
                price_info_object,
                update
            );
        }
```

**File:** target_chains/sui/contracts/sources/pyth.move (L337-337)
```text
        update_timestamp > cached_timestamp
```

**File:** target_chains/sui/contracts/sources/pyth.move (L815-817)
```text

        // check fee coins are deposited in the price info object
        assert!(price_info::get_balance(&price_info_object_1)==DEFAULT_BASE_UPDATE_FEE, 0);
```

**File:** target_chains/sui/contracts/sources/price_info.move (L105-115)
```text
    public fun deposit_fee_coins(price_info_object: &mut PriceInfoObject, fee_coins: Coin<SUI>) {
        if (!dynamic_object_field::exists_with_type<vector<u8>, Coin<SUI>>(&price_info_object.id, FEE_STORAGE_KEY)) {
            dynamic_object_field::add(&mut price_info_object.id, FEE_STORAGE_KEY, fee_coins);
        }
        else {
            let current_fee = dynamic_object_field::borrow_mut<vector<u8>, Coin<SUI>>(
                &mut price_info_object.id,
                FEE_STORAGE_KEY
            );
            coin::join(current_fee, fee_coins);
        };
```
