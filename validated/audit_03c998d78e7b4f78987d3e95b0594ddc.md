### Title
Multi-Denomination Fee Amounts Aggregated Without Price Conversion in Osmosis Fee Sufficiency Check - (File: `target_chains/cosmwasm/contracts/pyth/src/contract.rs`)

### Summary

The Osmosis-specific `is_fee_sufficient` function in the Pyth CosmWasm contract aggregates raw token amounts across multiple different denominations without any exchange-rate conversion, then compares the raw sum against the required fee denominated in the base token (`uosmo`). This allows an unprivileged caller to satisfy the fee check by sending a sufficient raw quantity of a lower-value allowed token (e.g., `uion`) instead of the required base denomination, effectively underpaying the protocol fee.

### Finding Description

In `target_chains/cosmwasm/contracts/pyth/src/contract.rs`, the `#[cfg(feature = "osmosis")]` variant of `is_fee_sufficient` (lines 163–198) iterates over all coins sent by the caller, validates each coin's denomination against the Osmosis txfee module's allowed list, and then **adds their raw `u128` amounts together into a single `total_amount`** regardless of denomination:

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

The required fee (`base_denom_fee`) is expressed in `uosmo`. The `total_amount` is a denomination-blind sum of all sent coins. No spot-price conversion is applied. The code itself acknowledges this with two inline comments:

> `// FIXME: should we accept fee for a single transaction in different tokens?`

> `// NOTE: the base fee denom right now is = denom: 'uosmo', amount: 1, which is almost negligible … this logic will be updated to use spot price for different valid tokens in future`

This is structurally identical to the reported analog: amounts of different coin denominations are compared/aggregated without verifying they represent equivalent value.

### Impact Explanation

An unprivileged caller submitting price updates to the Pyth contract on Osmosis can pay the required fee entirely in an allowed alternative token (e.g., `uion`) whose market value is lower than `uosmo`. The raw numeric amount check passes, but the protocol receives less economic value than intended. If fees are raised in the future (currently set to 1 uosmo per VAA, which is negligible), this becomes a meaningful fee-bypass vector. Additionally, a caller can mix denominations — sending, for example, 0 `uosmo` + N `uion` — and the sum satisfies the check even though no base-denomination tokens were paid.

### Likelihood Explanation

The Osmosis deployment of the Pyth contract is live on mainnet. Any user calling `UpdatePriceFeeds` can trigger this path. No privileged access is required. The code's own FIXME comments confirm the developers are aware the conversion logic is missing and deferred. The entry path is direct: call `execute(UpdatePriceFeeds { data })` with `info.funds` containing only an allowed non-base-denom token.

### Recommendation

Before comparing against `base_denom_fee`, convert each coin's amount to its `uosmo` equivalent using the Osmosis TxFees module's spot price (via `TxfeesQuerier` or a pool price query). Only after normalizing all sent coins to the base denomination should the total be compared against the required fee. Until this conversion is implemented, the contract should restrict fee payment to the single base denomination (`uosmo`) on Osmosis, removing the multi-denom aggregation path entirely.

### Proof of Concept

1. Deploy or interact with the Pyth contract on Osmosis mainnet (contract compiled with `feature = "osmosis"`).
2. Query `GetUpdateFee` for N VAAs — returns `Coin { amount: N, denom: "uosmo" }`.
3. Call `UpdatePriceFeeds { data: [vaa1, ...] }` with `info.funds = [Coin { amount: N, denom: "uion" }]`.
4. Inside `is_fee_sufficient`: `uion` passes `is_allowed_tx_fees_denom` (it is in the Osmosis txfee module), `total_amount = N`, `base_denom_fee.amount = N`, check `N <= N` → `true`.
5. Price feeds are updated; the protocol received `N uion` instead of `N uosmo`, with no exchange-rate adjustment applied. [1](#0-0) [2](#0-1) [3](#0-2)

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
