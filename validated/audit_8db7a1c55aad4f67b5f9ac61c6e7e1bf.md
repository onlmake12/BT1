Looking at the exact code path in `pyth.move` and `price_info.move`:

### Title
Fee Consumed Without Price Update on Equal-Timestamp Submission — (`target_chains/sui/contracts/sources/pyth.move`)

---

### Summary

In `pyth::pyth::update_single_price_feed`, the caller's fee is deposited into the `PriceInfoObject` **unconditionally and before** the freshness check. When a front-runner (or any concurrent updater) submits an update with the same Pythnet-attested timestamp `T` first, the subsequent legitimate updater's fee is permanently consumed while no price state change occurs and no `PriceFeedUpdateEvent` is emitted — directly violating the invariant that a fee must only be consumed when a price update is successfully applied.

---

### Finding Description

The execution order in `update_single_price_feed` is:

1. **Line 274** — fee sufficiency check (passes normally).
2. **Line 277** — `price_info::deposit_fee_coins(price_info_object, fee)` — fee is **irrevocably joined** into the `PriceInfoObject`'s dynamic field.
3. **Lines 283–291** — matching `PriceInfo` is found and `update_cache` is called.
4. Inside `update_cache` (**line 316**) — `is_fresh_update` is evaluated.
5. `is_fresh_update` (**line 337**) returns `update_timestamp > cached_timestamp` — **strict greater-than**.

If the front-runner has already applied an update with timestamp `T`, the cached timestamp is now `T`. The legitimate updater's update also carries timestamp `T` (same VAA / same Pythnet slot). The check evaluates `T > T = false`, so neither `update_price_info_object` nor `emit_price_feed_update` is called. The function returns normally — **no abort, no refund**.

`deposit_fee_coins` in `price_info.move` (lines 105–116) uses `coin::join` to merge the coins into the object's dynamic field. There is no corresponding `withdraw_fee_coins` function anywhere in the module; the coins are permanently locked inside the `PriceInfoObject`.

**Attack path (unprivileged, no special role):**

1. Attacker observes the raw VAA bytes in the victim's pending transaction (Sui validators see submitted transactions; the VAA bytes are public data on Wormhole anyway).
2. Attacker calls `vaa::parse_and_verify` on the same bytes to obtain their own `VAA` object (no restriction on who may call this).
3. Attacker calls `create_price_infos_hot_potato` / `create_authenticated_price_infos_using_accumulator` with their VAA → gets a `HotPotatoVector<PriceInfo>` containing timestamp `T`.
4. Attacker calls `update_single_price_feed` — price feed is updated to `T`, attacker's fee is deposited.
5. Victim's transaction executes: fee deposited at line 277, `is_fresh_update` returns `false`, no update, no event, **victim's fee is locked**.

The attacker spends one fee to cause the victim to lose one fee. Repeated across many victims or price feeds, this is a scalable griefing/drain attack.

---

### Impact Explanation

- The victim's `Coin<SUI>` is permanently merged into the `PriceInfoObject` with no recovery path.
- No price state change occurs for the victim's submission.
- No `PriceFeedUpdateEvent` is emitted, so off-chain indexers see no update from the victim's transaction.
- The invariant "fee is only consumed when a price update is successfully applied" is broken.
- This constitutes **direct, irreversible loss of user funds** within the stated scope.

---

### Likelihood Explanation

- Sui does not have a public mempool in the Ethereum sense, but submitted transactions are visible to validators before ordering. A validator-colluding attacker or a well-connected node can observe and front-run within the same checkpoint.
- More practically, **no deliberate front-running is required**: any two independent updaters who both fetch the same fresh VAA and submit concurrently will race; the second one always loses their fee. This is a normal operational scenario (multiple relayers, multiple dApps updating the same feed).
- The VAA bytes are public (posted on Wormhole), so the attacker does not need to intercept anything private.
- The attack is **local-testable** with a single-process state-transition test as described in the proof of concept.

---

### Recommendation

Move the fee deposit **after** the freshness check, or refund the fee when `is_fresh_update` returns `false`:

```move
// Option A: deposit only if fresh
if (is_fresh_update(cur_price_info, price_info_object)) {
    price_info::deposit_fee_coins(price_info_object, fee);
    // emit + update ...
} else {
    transfer::public_transfer(fee, tx_context::sender(ctx)); // refund
}

// Option B: abort on stale (makes the no-op explicit to callers)
assert!(is_fresh_update(cur_price_info, price_info_object), E_STALE_PRICE_UPDATE);
price_info::deposit_fee_coins(price_info_object, fee);
```

---

### Proof of Concept

```
State: PriceInfoObject cached at timestamp T-1.

Tx 1 (attacker):
  vaa_A = parse_and_verify(raw_vaa_bytes_T)   // timestamp T
  hot = create_price_infos_hot_potato(vaa_A)
  update_single_price_feed(hot, pio, fee_A)
  → is_fresh_update: T > T-1 = true → price updated, fee_A deposited ✓

Tx 2 (victim, same raw_vaa_bytes_T):
  vaa_V = parse_and_verify(raw_vaa_bytes_T)   // timestamp T
  hot = create_price_infos_hot_potato(vaa_V)
  update_single_price_feed(hot, pio, fee_V)
  → deposit_fee_coins called: fee_V locked in PriceInfoObject
  → is_fresh_update: T > T = false → no update, no event
  → fee_V permanently lost, cached price unchanged from Tx 1

Assert: balance(pio) == fee_A + fee_V, cached_timestamp == T, no PriceFeedUpdateEvent from Tx 2.
```

The root cause is at: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** target_chains/sui/contracts/sources/pyth.move (L335-337)
```text
        let cached_timestamp = price::get_timestamp(&price_feed::get_price(cached_price_feed));

        update_timestamp > cached_timestamp
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
