### Title
Multi-Token Fee Equivalence Without Price Normalization Allows Fee Underpayment on Osmosis - (`File: target_chains/cosmwasm/contracts/pyth/src/contract.rs`)

---

### Summary

The Osmosis-specific `is_fee_sufficient` function in the Pyth CosmWasm contract accepts multiple token denominations as payment for price update fees, but sums their raw amounts without any price conversion. This means a caller can pay the required fee using any Osmosis-allowed transaction fee token (e.g., `uion`, IBC tokens) at a 1:1 raw unit ratio with `uosmo`, regardless of the actual market value of those tokens.

---

### Finding Description

In `target_chains/cosmwasm/contracts/pyth/src/contract.rs`, the `is_fee_sufficient` function (compiled only with the `osmosis` feature flag) iterates over all coins sent in `info.funds`, validates each denom against the Osmosis txfee module, and then **adds their raw amounts together** without any price conversion:

```rust
let mut total_amount = 0u128;
for coin in &info.funds {
    if coin.denom != state.fee.denom && !is_allowed_tx_fees_denom(deps, &coin.denom) {
        return Err(PythContractError::InvalidFeeDenom { ... })?;
    }
    total_amount = total_amount.checked_add(coin.amount.u128())...;
}
let base_denom_fee = get_update_fee(deps, data)?;
Ok(base_denom_fee.amount.u128() <= total_amount)
```

The `base_denom_fee` is denominated in `uosmo` (the base denom), but `total_amount` is the raw sum of all coins regardless of their denomination. The code itself acknowledges this is incomplete:

> `// NOTE: the base fee denom right now is = denom: 'uosmo', amount: 1, which is almost negligible`
> `// It's not important to convert the price right now. For now`
> `// we are keeping the base fee amount same for each valid denom -> 1`
> `// but this logic will be updated to use spot price for different valid tokens in future`

The same flaw exists in `get_update_fee_for_denom`, which returns the same raw fee amount regardless of which denom is requested:

> `// for other valid denoms too we are using the base amount as 1`
> `// this will be change later on to add custom logic using spot price for valid tokens` [1](#0-0) [2](#0-1) 

---

### Impact Explanation

Any unprivileged caller invoking `UpdatePriceFeeds` on the Osmosis deployment can pay the required fee using any Osmosis-allowed transaction fee token at a 1:1 raw unit ratio with `uosmo`. If an allowed token (e.g., `uion` or an IBC-bridged token) has a lower market value per micro-unit than `uosmo`, the caller pays less in real economic value than the protocol intends to collect.

The financial impact is currently low because the configured fee is 1 `uosmo` per update (approximately $0.000001), making the absolute underpayment negligible. However, if governance raises the fee to a meaningful amount, the vulnerability becomes economically significant: a caller could pay with a low-value allowed token and satisfy the fee check while paying a fraction of the intended USD value. [3](#0-2) 

---

### Likelihood Explanation

The entry path is fully unprivileged: any account on Osmosis can call `UpdatePriceFeeds` with `info.funds` containing only `uion` or an IBC token. The `is_allowed_tx_fees_denom` check only verifies that the denom is registered in Osmosis's txfee module — it does not enforce any price parity. The vulnerability is reachable on every call to `update_price_feeds` on the Osmosis deployment. [4](#0-3) [5](#0-4) 

---

### Recommendation

Replace the raw-amount summation with a spot-price-converted sum. For each non-base denom coin in `info.funds`, query the Osmosis `TxfeesQuerier` (or a TWAP/spot price oracle) to convert the coin's amount to its `uosmo` equivalent before adding it to `total_amount`. Only compare the `uosmo`-equivalent total against `base_denom_fee.amount`. The code already contains the correct intended algorithm in comments:

```
// how to change this in future
// for given coins verify they are allowed in txfee module
// convert each of them to the base token that is 'uosmo'
// combine all the converted token
// check with `has_coins`
```

Implement this conversion before the fee check is used in production with non-negligible fee amounts. [6](#0-5) 

---

### Proof of Concept

Assume the Osmosis Pyth contract is configured with `fee = { denom: "uosmo", amount: 100 }` (100 uosmo per update). Suppose `uion` is worth 0.01 uosmo per unit.

1. Attacker calls `UpdatePriceFeeds` with `info.funds = [{ denom: "uion", amount: 100 }]`.
2. `is_allowed_tx_fees_denom` returns `true` for `uion` (it is registered in Osmosis txfee module).
3. `total_amount = 100` (raw uion units).
4. `base_denom_fee.amount = 100` (uosmo units).
5. Check: `100 <= 100` → `true` → fee accepted.
6. Attacker paid 100 uion ≈ 1 uosmo in real value, but the protocol expected 100 uosmo.

The attacker paid 1% of the intended fee value. The test suite confirms this behavior is the current expected outcome:

```rust
// a valid denom other than base denom with sufficient fee
info.funds = coins(100, "uion");
let result = is_fee_sufficient(deps, info.clone(), data);
assert_eq!(result, Ok(true));
``` [7](#0-6)

### Citations

**File:** target_chains/cosmwasm/contracts/pyth/src/contract.rs (L144-161)
```rust
fn is_allowed_tx_fees_denom(deps: &Deps, denom: &String) -> bool {
    // TxFeesQuerier uses stargate queries which we can't mock as of now.
    // The capability has not been implemented in `cosmwasm-std` yet.
    // Hence, we are hacking it with a feature flag to be able to write tests.
    // FIXME
    #[cfg(test)]
    if denom == "uion"
        || denom == "ibc/FF3065989E34457F342D4EFB8692406D49D4E2B5C70F725F127862E22CE6BDCD"
    {
        return true;
    }

    let querier = TxfeesQuerier::new(&deps.querier);
    match querier.denom_pool_id(denom.to_string()) {
        Ok(_) => true,
        Err(_) => false,
    }
}
```

**File:** target_chains/cosmwasm/contracts/pyth/src/contract.rs (L163-198)
```rust
// TODO: add tests for these
#[cfg(feature = "osmosis")]
fn is_fee_sufficient(deps: &Deps, info: MessageInfo, data: &[Binary]) -> StdResult<bool> {
    let state = config_read(deps.storage).load()?;

    // how to change this in future
    // for given coins verify they are allowed in txfee module
    // convert each of them to the base token that is 'uosmo'
    // combine all the converted token
    // check with `has_coins`

    // FIXME: should we accept fee for a single transaction in different tokens?
    let mut total_amount = 0u128;
    for coin in &info.funds {
        if coin.denom != state.fee.denom && !is_allowed_tx_fees_denom(deps, &coin.denom) {
            return Err(PythContractError::InvalidFeeDenom {
                denom: coin.denom.to_string(),
            })?;
        }
        total_amount = total_amount
            .checked_add(coin.amount.u128())
            .ok_or(OverflowError::new(
                OverflowOperation::Add,
                total_amount,
                coin.amount,
            ))?;
    }

    let base_denom_fee = get_update_fee(deps, data)?;

    // NOTE: the base fee denom right now is = denom: 'uosmo', amount: 1, which is almost negligible
    // It's not important to convert the price right now. For now
    // we are keeping the base fee amount same for each valid denom -> 1
    // but this logic will be updated to use spot price for different valid tokens in future
    Ok(base_denom_fee.amount.u128() <= total_amount)
}
```

**File:** target_chains/cosmwasm/contracts/pyth/src/contract.rs (L205-213)
```rust
fn update_price_feeds(
    mut deps: DepsMut,
    env: Env,
    info: MessageInfo,
    data: &[Binary],
) -> StdResult<Response<MsgWrapper>> {
    if !is_fee_sufficient(&deps.as_ref(), info, data)? {
        Err(PythContractError::InsufficientFee)?;
    }
```

**File:** target_chains/cosmwasm/contracts/pyth/src/contract.rs (L717-736)
```rust
#[cfg(feature = "osmosis")]
/// Osmosis can support multiple tokens for transaction fees
/// This will return update fee for the given denom only if that denom is allowed in Osmosis's txFee module
/// Else it will throw error
pub fn get_update_fee_for_denom(deps: &Deps, vaas: &[Binary], denom: String) -> StdResult<Coin> {
    let config = config_read(deps.storage).load()?;

    // if the denom is not a base denom it should be an allowed one
    if denom != config.fee.denom && !is_allowed_tx_fees_denom(deps, &denom) {
        return Err(PythContractError::InvalidFeeDenom { denom })?;
    }

    // the base fee is set to -> denom = base denom of a chain, amount = 1
    // which is very minimal
    // for other valid denoms too we are using the base amount as 1
    // base amount is multiplied to number of vaas to get the total amount

    // this will be change later on to add custom logic using spot price for valid tokens
    Ok(coin(get_update_fee_amount(deps, vaas)?, denom))
}
```

**File:** target_chains/cosmwasm/contracts/pyth/src/contract.rs (L997-1017)
```rust
    #[cfg(feature = "osmosis")]
    fn check_sufficient_fee(deps: &Deps, data: &[Binary]) {
        let mut info = mock_info("123", coins(100, "foo").as_slice());
        let result = is_fee_sufficient(deps, info.clone(), data);
        assert_eq!(result, Ok(true));

        // insufficient fee in base denom -> false
        info.funds = coins(50, "foo");
        let result = is_fee_sufficient(deps, info.clone(), data);
        assert_eq!(result, Ok(false));

        // valid denoms are 'uion' or 'ibc/FF3065989E34457F342D4EFB8692406D49D4E2B5C70F725F127862E22CE6BDCD'
        // a valid denom other than base denom with sufficient fee
        info.funds = coins(100, "uion");
        let result = is_fee_sufficient(deps, info.clone(), data);
        assert_eq!(result, Ok(true));

        // insufficient fee in valid denom -> false
        info.funds = coins(50, "uion");
        let result = is_fee_sufficient(deps, info.clone(), data);
        assert_eq!(result, Ok(false));
```
