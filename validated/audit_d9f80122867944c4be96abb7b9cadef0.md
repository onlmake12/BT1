### Title
Excess Fee Coin Permanently Deposited to Pyth Contract Without Returning Change to Caller - (File: target_chains/aptos/contracts/sources/pyth.move)

### Summary

The `update_price_feeds` function in the Aptos Pyth contract accepts a `Coin<AptosCoin>` fee parameter, validates only that it is sufficient (`update_fee <= coin::value(&fee)`), but then unconditionally deposits the **entire** coin to the Pyth contract address — including any excess above the required fee. The excess is permanently lost to the caller with no user-accessible recovery path.

### Finding Description

In `target_chains/aptos/contracts/sources/pyth.move`, the `update_price_feeds` function:

```move
public fun update_price_feeds(vaas: vector<vector<u8>>, fee: Coin<AptosCoin>) {
    let total_updates = 0;
    while (!vector::is_empty(&vaas)) {
        total_updates = total_updates + update_price_feed_from_single_vaa(vector::pop_back(&mut vaas));
    };
    let update_fee = state::get_base_update_fee() * total_updates;
    assert!(update_fee <= coin::value(&fee), error::insufficient_fee());
    coin::deposit(@pyth, fee);   // <-- entire coin deposited, not just update_fee
}
``` [1](#0-0) 

The function computes `update_fee = base_update_fee * total_updates` and asserts the provided coin is at least that large, but then calls `coin::deposit(@pyth, fee)` which deposits the **full** coin value — not just `update_fee`. Any amount above `update_fee` is silently absorbed into the Pyth contract's balance and is inaccessible to the caller.

The correct implementation would split the coin before depositing:

```move
let (exact_fee, excess) = coin::split(fee, update_fee);
coin::deposit(@pyth, exact_fee);
coin::deposit(caller_address, excess);  // return excess
```

The same flaw propagates through `update_price_feeds_if_fresh`, which delegates directly to `update_price_feeds`:

```move
assert!(fresh_data, error::no_fresh_data());
update_price_feeds(vaas, fee);
``` [2](#0-1) 

By contrast, `update_price_feeds_with_funder` correctly withdraws only the exact required amount:

```move
let update_fee = state::get_base_update_fee() * total_updates;
let fee = coin::withdraw<AptosCoin>(account, update_fee);
coin::deposit(@pyth, fee);
``` [3](#0-2) 

This inconsistency between the two entry points is the root cause.

### Impact Explanation

Any caller of `update_price_feeds` who passes a `Coin` with value exceeding the exact required fee permanently loses the excess. The Pyth contract accumulates these excess coins in its balance. There is no user-facing function to reclaim them; recovery requires privileged governance action. As call volume grows, the aggregate loss to callers grows proportionally.

### Likelihood Explanation

This is triggered whenever a caller passes more than the exact required fee. Realistic scenarios include:

1. **Fee change between estimation and execution**: A caller calls `get_update_fee` to estimate the fee, the governance updates `base_update_fee` before the transaction lands, and the caller's pre-computed coin is now larger than required.
2. **Caller over-provisions defensively**: Callers may intentionally pass a slightly larger coin to avoid reverts, analogous to the common EVM pattern of sending slightly more `msg.value` than required.
3. **Integrator error**: Downstream protocols integrating Pyth on Aptos may compute fees independently and pass a rounded-up amount.

### Recommendation

Split the coin before depositing, returning the excess to the caller:

```move
public fun update_price_feeds(vaas: vector<vector<u8>>, fee: Coin<AptosCoin>) {
    let total_updates = 0;
    while (!vector::is_empty(&vaas)) {
        total_updates = total_updates + update_price_feed_from_single_vaa(vector::pop_back(&mut vaas));
    };
    let update_fee = state::get_base_update_fee() * total_updates;
    assert!(update_fee <= coin::value(&fee), error::insufficient_fee());
    let excess_value = coin::value(&fee) - update_fee;
    if (excess_value > 0) {
        let excess = coin::extract(&mut fee, excess_value);
        coin::deposit(caller_address, excess);
    };
    coin::deposit(@pyth, fee);
}
```

Alternatively, enforce an exact-match check (`update_fee == coin::value(&fee)`) so callers are forced to pass the precise amount, consistent with how `update_price_feeds_with_funder` operates.

### Proof of Concept

1. Governance sets `base_update_fee = 50`.
2. Caller calls `get_update_fee(&TEST_VAAS)` → returns `50`.
3. Caller mints `75` coins and calls `update_price_feeds(TEST_VAAS, coins_75)`.
4. `total_updates = 1`, `update_fee = 50`, assertion passes (`50 <= 75`).
5. `coin::deposit(@pyth, coins_75)` — all 75 coins deposited.
6. Caller loses 25 coins with no recovery path.

This is confirmed by the test setup pattern in the codebase, where `initial_balance = 75` and `update_fee = 50` are used together: [4](#0-3) 

The test at line 757 passes `coins` minted to `100` against an `update_fee` of `50`, and the assertion at line 1088 only checks that the funder's balance decreased by `update_fee` — it does not verify that the Pyth contract received only `update_fee`. The `update_price_feeds` (non-funder) path has no equivalent assertion guarding against excess absorption.

### Citations

**File:** target_chains/aptos/contracts/sources/pyth.move (L147-157)
```text
    public entry fun update_price_feeds_with_funder(account: &signer, vaas: vector<vector<u8>>) {
        let total_updates = 0;
        // Update the price feed from each VAA
        while (!vector::is_empty(&vaas)) {
            total_updates = total_updates + update_price_feed_from_single_vaa(vector::pop_back(&mut vaas));
        };
        // Charge the message update fee
        let update_fee = state::get_base_update_fee() * total_updates;
        let fee = coin::withdraw<AptosCoin>(account, update_fee);
        coin::deposit(@pyth, fee);
    }
```

**File:** target_chains/aptos/contracts/sources/pyth.move (L170-180)
```text
    public fun update_price_feeds(vaas: vector<vector<u8>>, fee: Coin<AptosCoin>) {
        let total_updates = 0;
        // Update the price feed from each VAA
        while (!vector::is_empty(&vaas)) {
            total_updates = total_updates + update_price_feed_from_single_vaa(vector::pop_back(&mut vaas));
        };
        // Charge the message update fee
        let update_fee = state::get_base_update_fee() * total_updates;
        assert!(update_fee <= coin::value(&fee), error::insufficient_fee());
        coin::deposit(@pyth, fee);
    }
```

**File:** target_chains/aptos/contracts/sources/pyth.move (L349-379)
```text
    public entry fun update_price_feeds_if_fresh(
        vaas: vector<vector<u8>>,
        price_identifiers: vector<vector<u8>>,
        publish_times: vector<u64>,
        fee: Coin<AptosCoin>) {

        assert!(vector::length(&price_identifiers) == vector::length(&publish_times),
            error::invalid_publish_times_length());

        let fresh_data = false;
        let i = 0;
        while (i < vector::length(&publish_times)) {
            let price_identifier = price_identifier::from_byte_vec(
                *vector::borrow(&price_identifiers, i));
            if (!state::price_info_cached(price_identifier)) {
                fresh_data = true;
                break
            };

            let cached_timestamp = price::get_timestamp(&get_price_unsafe(price_identifier));
            if (cached_timestamp < *vector::borrow(&publish_times, i)) {
                fresh_data = true;
                break
            };

            i = i + 1;
        };

        assert!(fresh_data, error::no_fresh_data());
        update_price_feeds(vaas, fee);
    }
```

**File:** target_chains/aptos/contracts/sources/pyth.move (L756-767)
```text
    fun test_update_price_feeds_success(aptos_framework: &signer) {
        let (burn_capability, mint_capability, coins) = setup_test(aptos_framework, 500, 1, x"5d1f252d5de865279b00c84bce362774c2804294ed53299bc4a0389a5defef92", data_sources_for_test_vaa(), 50, 100);

        // Update the price feeds from the VAA
        pyth::update_price_feeds(TEST_VAAS, coins);

        // Check that the cache has been updated
        let expected = get_mock_price_infos();
        check_price_feeds_cached(&expected);

        cleanup_test(burn_capability, mint_capability);
    }
```
