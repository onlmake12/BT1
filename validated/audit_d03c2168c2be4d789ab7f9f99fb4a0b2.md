### Title
Osmosis Multi-Denom Fee Validation Sums Raw Amounts Without Price Conversion — (`File: target_chains/cosmwasm/contracts/pyth/src/contract.rs`)

---

### Summary

The Osmosis-specific `is_fee_sufficient` function in the Pyth CosmWasm contract accepts multiple fee denominations but sums their raw token amounts without any price conversion. This is the direct analog of the StableVault H-08 finding: multiple assets with different market values are treated as 1:1 equivalent, allowing a caller to satisfy the fee requirement by paying with a cheaper token at the same raw-amount threshold.

---

### Finding Description

The Osmosis deployment of the Pyth CosmWasm contract supports multiple fee denominations via the Osmosis `txfee` module. The `is_fee_sufficient` function (compiled only when `feature = "osmosis"`) iterates over all coins sent by the caller, validates each denom against the allowed list, and then **adds their raw `u128` amounts together into a single `total_amount`**:

```rust
let mut total_amount = 0u128;
for coin in &info.funds {
    if coin.denom != state.fee.denom && !is_allowed_tx_fees_denom(deps, &coin.denom) {
        return Err(...)?;
    }
    total_amount = total_amount.checked_add(coin.amount.u128())...;
}
let base_denom_fee = get_update_fee(deps, data)?;
Ok(base_denom_fee.amount.u128() <= total_amount)
``` [1](#0-0) 

The comparison `base_denom_fee.amount.u128() <= total_amount` treats 1 `uion` as equivalent to 1 `uosmo` in value. The code itself acknowledges this is wrong and deferred:

> *"NOTE: the base fee denom right now is = denom: 'uosmo', amount: 1 … For now we are keeping the base fee amount same for each valid denom → 1 but this logic will be updated to use spot price for different valid tokens in future"* [2](#0-1) 

The same assumption is baked into `get_update_fee_for_denom`, which returns the same raw amount regardless of which denom is requested: [3](#0-2) 

The intended correct logic is even described in the comments but never implemented:

> *"how to change this in future: for given coins verify they are allowed in txfee module, convert each of them to the base token that is 'uosmo', combine all the converted token, check with `has_coins`"* [4](#0-3) 

---

### Impact Explanation

Any unprivileged caller invoking `update_price_feeds` on the Osmosis deployment can pay the required fee using any Osmosis-allowed token (e.g., `uion`, IBC-wrapped assets) at a 1:1 raw-amount ratio to `uosmo`. If the chosen token is worth less than `uosmo` at the time of the call, the protocol receives less economic value than the configured fee requires. A caller can also split payment across denominations (e.g., 50 `uosmo` + 50 `uion` to satisfy a 100 `uosmo` fee), with the cheaper token portion representing an underpayment.

The practical financial loss per call is currently negligible because the configured fee is 1 `uosmo` per update (≈ $0.000001). However, the fee is governance-configurable; if it is raised to a meaningful amount, the underpayment gap scales proportionally. The vulnerability is structural and persistent regardless of fee level.

---

### Likelihood Explanation

The entry path is fully unprivileged: any account on Osmosis can call `ExecuteMsg::UpdatePriceFeeds` with funds denominated in any Osmosis-allowed token. No special role, key, or governance action is required. The Osmosis `txfee` module already maintains the list of allowed denoms, so the attacker only needs to hold any token on that list that trades at a discount to `uosmo`.

---

### Recommendation

Implement the conversion logic already described in the code comments. Before summing, query the Osmosis `txfee` spot-price oracle (or the `TxfeesQuerier`) to convert each non-base-denom coin amount into its `uosmo` equivalent, then compare the converted total against `base_denom_fee.amount`. The `get_update_fee_for_denom` query should similarly return the fee amount scaled by the spot price of the requested denom relative to `uosmo`.

---

### Proof of Concept

1. Query `GetUpdateFee` for 1 VAA → returns `{ amount: 1, denom: "uosmo" }`.
2. Observe that `uion` currently trades at a discount to `uosmo` on Osmosis (verifiable via the Osmosis DEX).
3. Call `ExecuteMsg::UpdatePriceFeeds` with `funds: [{ amount: 1, denom: "uion" }]`.
4. `is_fee_sufficient` checks: `is_allowed_tx_fees_denom("uion") == true`, `total_amount = 1`, `base_denom_fee.amount = 1`, `1 <= 1` → returns `true`.
5. The price feed is updated. The protocol received 1 `uion` instead of 1 `uosmo`, which is worth less.
6. Scale with a higher governance-set fee (e.g., 1000 `uosmo` per update) and the underpayment per call becomes `1000 × (price_uosmo − price_uion)` in USD terms. [5](#0-4)

### Citations

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

**File:** target_chains/cosmwasm/contracts/pyth/src/contract.rs (L729-735)
```rust
    // the base fee is set to -> denom = base denom of a chain, amount = 1
    // which is very minimal
    // for other valid denoms too we are using the base amount as 1
    // base amount is multiplied to number of vaas to get the total amount

    // this will be change later on to add custom logic using spot price for valid tokens
    Ok(coin(get_update_fee_amount(deps, vaas)?, denom))
```
