### Title
Osmosis Multi-Token Fee Check Assumes 1:1 Value Between Denoms, Allowing Underpayment - (File: `target_chains/cosmwasm/contracts/pyth/src/contract.rs`)

### Summary

The Osmosis-specific `is_fee_sufficient` function in the Pyth CosmWasm contract sums raw token amounts across different denominations and compares them directly to the base `uosmo` fee amount, without any exchange-rate conversion. Any unprivileged caller can satisfy the fee check by sending an alternative allowed token (e.g., `uion`) whose raw unit count equals the required `uosmo` amount, even if that token is worth far less in USD terms.

### Finding Description

In `target_chains/cosmwasm/contracts/pyth/src/contract.rs`, the Osmosis feature-gated `is_fee_sufficient` function (lines 163–198) accepts multiple token denominations as payment. It iterates over all coins in `info.funds`, validates each denom against the allowed list, and accumulates their raw amounts into a single `total_amount` counter. It then compares this counter against `base_denom_fee.amount` (the fee denominated in `uosmo`):

```rust
// NOTE: the base fee denom right now is = denom: 'uosmo', amount: 1, which is almost negligible
// It's not important to convert the price right now. For now
// we are keeping the base fee amount same for each valid denom -> 1
// but this logic will be updated to use spot price for different valid tokens in future
Ok(base_denom_fee.amount.u128() <= total_amount)
```

No exchange-rate conversion is applied. The same flaw exists in `get_update_fee_for_denom` (lines 721–735), which returns the identical raw `uosmo` amount for any valid denom:

```rust
// this will be change later on to add custom logic using spot price for valid tokens
Ok(coin(get_update_fee_amount(deps, vaas)?, denom))
```

Both functions are explicitly marked with `TODO`/`FIXME` comments acknowledging the missing spot-price conversion.

### Impact Explanation

**Impact: Low**

An unprivileged caller on the Osmosis deployment can call `update_price_feeds` (or `parse_price_feed_updates`) and pay the fee using an alternative allowed token (e.g., `uion` or the IBC-wrapped token `ibc/FF3065...`) whose market value is lower than `uosmo`. Since the check compares raw unit counts without conversion, the caller satisfies the fee requirement while paying less USD value than intended. The protocol collects less revenue than configured. The practical financial impact is bounded by the current fee level (1 `uosmo` per update, which is negligible), but the accounting invariant is broken and the severity scales if the fee is raised.

### Likelihood Explanation

**Likelihood: Medium**

The Osmosis deployment is live on mainnet. The `update_price_feeds` entry point is permissionless — any transaction sender can call it. The alternative denoms (`uion`, IBC tokens) are explicitly whitelisted by `is_allowed_tx_fees_denom` via Osmosis's `TxFeesQuerier`. No privileged access or key compromise is required. The attacker only needs to hold any whitelisted alternative token and submit a standard `UpdatePriceFeeds` message.

### Recommendation

Apply spot-price conversion before comparing amounts. The comment in the code already describes the correct future approach:

> "convert each of them to the base token that is 'uosmo', combine all the converted token, check with `has_coins`"

Use Osmosis's `SpotPriceQuerier` or `TwapQuerier` to convert each non-base coin's amount to its `uosmo` equivalent before accumulating into `total_amount`. Similarly, `get_update_fee_for_denom` should return the fee amount scaled by the spot price of the requested denom relative to `uosmo`.

### Proof of Concept

1. The Osmosis Pyth contract is deployed with `fee = Coin { denom: "uosmo", amount: 1 }`.
2. `uion` is listed as an allowed fee denom (whitelisted by Osmosis's txfee module).
3. Suppose `uion` trades at 0.001 `uosmo` (i.e., 1 `uion` = 0.001 `uosmo`).
4. Attacker calls `update_price_feeds` with `info.funds = [Coin { denom: "uion", amount: 1 }]`.
5. `is_fee_sufficient` computes `total_amount = 1` and `base_denom_fee.amount = 1`.
6. The check `1 <= 1` passes — the attacker has paid 0.001 `uosmo` worth of value instead of 1 `uosmo`.
7. Price feeds are updated; the contract collects 1 `uion` ≈ $0.001× the intended fee.

The root cause is confirmed at: [1](#0-0) 

And the parallel issue in `get_update_fee_for_denom`: [2](#0-1)

### Citations

**File:** target_chains/cosmwasm/contracts/pyth/src/contract.rs (L175-197)
```rust
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
```

**File:** target_chains/cosmwasm/contracts/pyth/src/contract.rs (L721-735)
```rust
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
```
