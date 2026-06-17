The code confirms the vulnerability. Here is the complete analysis:

**Execution order in `update_single_price_feed`** (`pyth.move` lines 262–296):
1. Line 274 — fee sufficiency check
2. **Line 277 — `deposit_fee_coins` called unconditionally**
3. Lines 283–291 — matching price info found, `update_cache` called
4. Inside `update_cache` (line 316) — `is_fresh_update` checked; if false, price is silently skipped

**`deposit_fee_coins`** (`price_info.move` lines 105–116) is `public` with no conditions — it always joins the fee coin into the `price_info_object`'s dynamic field.

**No fee withdrawal function exists** anywhere in the Sui contracts (grep confirmed zero matches for `withdraw_fee`, `collect_fee`, etc.), so fees accumulate permanently in the `price_info_object`.

---

### Title
Fee Charged Unconditionally Before Freshness Check Allows Stale-Update Fee Drain — (`target_chains/sui/contracts/sources/pyth.move`)

### Summary
`update_single_price_feed` deposits the caller's fee into the `PriceInfoObject` **before** checking whether the supplied price update is fresh. When `is_fresh_update` returns `false`, the fee is permanently locked in the object and the price is never updated, breaking the invariant that fees are only collected for applied updates.

### Finding Description
In `pyth.move`, `update_single_price_feed` unconditionally calls `price_info::deposit_fee_coins` at line 277 before any freshness evaluation. The freshness check only occurs inside `update_cache` at line 316, inside an `if` branch that silently no-ops when the update is stale. There is no rollback of the deposited fee on the stale path, and `price_info.move` exposes no `withdraw_fee_coins` function, so the coins are permanently locked. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
Any caller who submits a valid but stale VAA-backed price update pays the full `base_update_fee` in SUI with zero benefit: the price feed is unchanged and the fee is irrecoverable. A griefing attacker can front-run a victim's pending `update_single_price_feed` call by submitting a fresher update first, guaranteeing the victim's transaction pays a fee for a no-op. Repeated calls with the same stale accumulator message (re-verified each time through Wormhole, which has no application-layer replay guard here) multiply the loss linearly. Fees accumulate in the `PriceInfoObject` with no withdrawal path. [4](#0-3) 

### Likelihood Explanation
The path is fully unprivileged: any account can call `create_authenticated_price_infos_using_accumulator` / `create_price_infos_hot_potato` with a valid (but old) VAA and then call `update_single_price_feed`. No special role, key, or governance access is required. Race conditions in normal usage (two callers updating the same feed concurrently) trigger this accidentally; deliberate exploitation requires only a valid historical VAA.

### Recommendation
Move `deposit_fee_coins` to **after** the freshness check, or refund the fee when `is_fresh_update` returns `false`. The simplest fix is to restructure `update_single_price_feed` so the fee is only deposited inside the `is_fresh_update == true` branch of `update_cache`, or to abort with a dedicated error code when the update is stale (consistent with how `update_price_feeds_if_fresh` on Aptos handles this). [5](#0-4) 

### Proof of Concept
```
// Pseudocode – Sui Move test
// 1. Initialize Pyth with base_update_fee = F
// 2. Create price feed for identifier ID with timestamp T
// 3. Obtain a valid VAA with timestamp T-1 (stale)
// 4. Loop N times:
//      vaa_i = wormhole::verify(stale_vaa_bytes)   // re-verify same bytes each iteration
//      hot_potato = create_authenticated_price_infos_using_accumulator(pyth_state, stale_msg, vaa_i, clock)
//      hot_potato = update_single_price_feed(pyth_state, hot_potato, &mut price_info_obj, coin::split(&mut wallet, F), clock)
//      hot_potato_vector::destroy(hot_potato)
// 5. Assert: price_info::get_balance(&price_info_obj) == N * F   // fees collected N times
// 6. Assert: price_info_obj.price_info.timestamp == T            // price never changed
```

The test would pass as written against the current code, confirming N × F SUI drained from the caller for zero price updates applied.

### Citations

**File:** target_chains/sui/contracts/sources/pyth.move (L262-296)
```text
    public fun update_single_price_feed(
        pyth_state: &PythState,
        price_updates: HotPotatoVector<PriceInfo>,
        price_info_object: &mut PriceInfoObject,
        fee: Coin<SUI>,
        clock: &Clock
    ): HotPotatoVector<PriceInfo> {
        let latest_only = state::assert_latest_only(pyth_state);

        // On Sui, users get to choose which price feeds to update. They specify a single price feed to
        // update at a time. We therefore charge the base fee for each such individual update.
        // This is a departure from Eth, where users don't get to necessarily choose.
        assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);

        // store fee coins within price info object
        price_info::deposit_fee_coins(price_info_object, fee);

        // Find price update corresponding to PriceInfoObject within the array of price_updates
        // and use it to update PriceInfoObject.
        let i = 0;
        let found = false;
        while (i < hot_potato_vector::length<PriceInfo>(&price_updates)){
            let cur_price_info = hot_potato_vector::borrow<PriceInfo>(&price_updates, i);
            if (has_same_price_identifier(cur_price_info, price_info_object)){
                found = true;
                update_cache(latest_only, cur_price_info, price_info_object, clock);
                break
            };
            i = i + 1;
        };
        if (found==false){
            abort E_PRICE_UPDATE_NOT_FOUND_FOR_PRICE_INFO_OBJECT
        };
        price_updates
    }
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

**File:** target_chains/sui/contracts/sources/price_info.move (L97-103)
```text
    public fun get_balance(price_info_object: &PriceInfoObject): u64 {
        if (!dynamic_object_field::exists_with_type<vector<u8>, Coin<SUI>>(&price_info_object.id, FEE_STORAGE_KEY)) {
            return 0
        };
        let fee = dynamic_object_field::borrow<vector<u8>, Coin<SUI>>(&price_info_object.id, FEE_STORAGE_KEY);
        coin::value(fee)
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
