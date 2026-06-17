The code flow is clear and the vulnerability is real. Let me confirm the exact sequence:

**`update_single_price_feed`** (lines 262–296):
1. Fee sufficiency check (line 274)
2. **Fee deposited unconditionally** (line 277)
3. Matching price identifier found in hot potato vector (lines 281–291)
4. `update_cache` called → `is_fresh_update` checked → if `false`, price is silently NOT updated (lines 316–322)

**`is_fresh_update`** (lines 327–338): returns `update_timestamp > cached_timestamp` — strictly greater than, so T_update == T_cached also fails.

**`deposit_fee_coins`** (price_info.move lines 105–116): unconditionally joins the fee coin into the `PriceInfoObject`'s dynamic field. No refund path exists.

---

### Title
Fee Consumed Without Price Update When Submitting Stale VAA — (`target_chains/sui/contracts/sources/pyth.move`)

### Summary
In `update_single_price_feed`, the user's SUI fee is deposited into the `PriceInfoObject` **before** the freshness check occurs. When the submitted VAA's price timestamp is not strictly greater than the cached timestamp, the fee is permanently consumed but the price feed state is unchanged.

### Finding Description
`update_single_price_feed` deposits the fee coin unconditionally at line 277, then calls `update_cache`, which internally calls `is_fresh_update`. If `is_fresh_update` returns `false` (T_update ≤ T_cached), the `if` branch at line 316 is simply skipped — no abort, no refund, no price write. The fee is already gone. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Impact Explanation
Every call to `update_single_price_feed` with a stale VAA (T_update ≤ T_cached) silently drains `base_update_fee` SUI from the caller into the `PriceInfoObject` with zero state change. There is no refund mechanism. This matches the scoped impact: **direct loss of user funds (SUI fee) with no genuine price update written**.

### Likelihood Explanation
The attack is trivially reachable by any unprivileged caller:
- Old Pyth VAAs are publicly available on-chain and off-chain.
- Wormhole replay protection operates at the VAA hash level, but an attacker can use any previously-unseen VAA whose embedded price timestamp is older than the current cached value (Pyth publishes hundreds of VAAs per second; many old ones are unused).
- Even without deliberate attack, a legitimate user whose transaction lands after a concurrent update in the same block suffers the same loss — making this a systemic fee-drain risk for all users.

### Recommendation
Move `deposit_fee_coins` to **after** the freshness check, or abort (refund by not consuming) when `is_fresh_update` returns `false`. The corrected logic in `update_cache` should either:
1. Return a boolean indicating whether the update was applied, and only deposit the fee in `update_single_price_feed` when `true`; or
2. Abort with a dedicated error code (e.g., `E_STALE_PRICE_UPDATE`) when the update is not fresh, so the entire transaction reverts and the fee is never taken.

### Proof of Concept
```
1. Deploy Pyth with base_update_fee = 50 MIST.
2. Call create_price_feeds with VAA_A (timestamp T_cached = 1000). PriceInfoObject now holds price at T=1000.
3. Obtain any old VAA_B (timestamp T_update = 999, valid data source, not previously consumed by Wormhole).
4. Call create_price_infos_hot_potato(VAA_B) → HotPotatoVector.
5. Call update_single_price_feed(hot_potato, price_info_object, fee=50, clock).
6. Assert: price_info::get_balance(price_info_object) increased by 50.
7. Assert: stored price timestamp is still 1000 (unchanged).
8. User lost 50 MIST; price feed unchanged. ✓
```

### Citations

**File:** target_chains/sui/contracts/sources/pyth.move (L274-277)
```text
        assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);

        // store fee coins within price info object
        price_info::deposit_fee_coins(price_info_object, fee);
```

**File:** target_chains/sui/contracts/sources/pyth.move (L315-322)
```text
        // Update the price info object with the new updated price info.
        if (is_fresh_update(update, price_info_object)){
            pyth_event::emit_price_feed_update(price_feed::from(price_info::get_price_feed(update)), clock::timestamp_ms(clock)/1000);
            price_info::update_price_info_object(
                price_info_object,
                update
            );
        }
```

**File:** target_chains/sui/contracts/sources/pyth.move (L327-338)
```text
    fun is_fresh_update(update: &PriceInfo, price_info_object: &PriceInfoObject): bool {
        // Get the timestamp of the update's current price
        let price_feed = price_info::get_price_feed(update);
        let update_timestamp = price::get_timestamp(&price_feed::get_price(price_feed));

        // Get the timestamp of the cached data for the price identifier
        let cached_price_info = price_info::get_price_info_from_price_info_object(price_info_object);
        let cached_price_feed =  price_info::get_price_feed(&cached_price_info);
        let cached_timestamp = price::get_timestamp(&price_feed::get_price(cached_price_feed));

        update_timestamp > cached_timestamp
    }
```

**File:** target_chains/sui/contracts/sources/price_info.move (L105-116)
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
    }
```
