### Title
Fee Overpayment When `defaultGasLimit` Is Not a Multiple of 10,000 in Entropy Provider Fee Calculation - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary
When an Entropy provider sets a `defaultGasLimit` that is not a multiple of 10,000, users requesting gas amounts in the range `(floor(defaultGasLimit/10000) * 10000, defaultGasLimit]` are charged an additional fee beyond the base `feeInWei`, even though they requested less gas than the default limit. This is a direct analog to the reported interval-boundary overpayment bug: integer rounding causes users to cross a threshold they should not cross, resulting in payment beyond the intended base amount.

### Finding Description
`setDefaultGasLimit` stores the raw `gasLimit` value without enforcing it is a multiple of 10,000. The call to `roundTo10kGas(gasLimit)` on line 931 is used only for bounds validation (MAX_GAS_LIMIT check); its return value is discarded and the raw value is stored: [1](#0-0) 

`getProviderFee` then rounds the *user's* requested gas limit up to the nearest 10k multiple via `roundTo10kGas`, and compares that rounded value against the raw `provider.defaultGasLimit`: [2](#0-1) 

When `provider.defaultGasLimit = 15000` (not a multiple of 10k) and a user requests `gasLimit = 10001`:
- `roundedGasLimit = roundTo10kGas(10001) * 10000 = 20000`
- `20000 > 15000` → additional fee branch is entered
- `additionalFee = (20000 − 15000) * feeInWei / 15000 = feeInWei / 3`
- **Total fee = 4/3 × feeInWei**

Yet a user requesting `gasLimit = 10000` pays only `feeInWei`. The user requesting one more unit of gas (still below `defaultGasLimit`) pays 33% more. The documented invariant — "Providers charge a minimum of their configured `feeInWei` for every request" — is violated. [3](#0-2) 

The `roundTo10kGas` function itself: [4](#0-3) 

### Impact Explanation
Users requesting gas amounts in the range `(floor(defaultGasLimit/10000) * 10000, defaultGasLimit]` are overcharged relative to the intended base fee. The maximum overpayment per request is `(10000 × feeInWei) / defaultGasLimit`. For providers with high `feeInWei` values, this is a meaningful financial loss for users. The user receives more gas than they requested (rounded up), but pays more than the protocol's stated base-fee guarantee.

### Likelihood Explanation
Provider registration is permissionless. Any actor can call `setDefaultGasLimit` with a non-multiple-of-10k value (e.g., 15000, 25000, 35000). A malicious provider can deliberately configure this to extract higher fees from users requesting gas in the affected range. Even a well-intentioned provider who sets a non-standard default (e.g., 50000 for a specific callback) inadvertently creates this overcharge window for users requesting 40001–50000 gas.

### Recommendation
Enforce that `defaultGasLimit` must be a multiple of 10,000 in `setDefaultGasLimit` by using the rounded value:

```solidity
uint32 roundedGasLimit = uint32(roundTo10kGas(gasLimit)) * TEN_THOUSAND;
provider.defaultGasLimit = roundedGasLimit;
```

Alternatively, use the rounded `defaultGasLimit` in the fee comparison inside `getProviderFee`:

```solidity
uint32 roundedDefault = uint32(roundTo10kGas(provider.defaultGasLimit)) * TEN_THOUSAND;
if (provider.defaultGasLimit > 0 && roundedGasLimit > roundedDefault) { ... }
```

### Proof of Concept
1. Provider calls `setDefaultGasLimit(15000)` — succeeds (15000 ≤ MAX_GAS_LIMIT).
2. User A calls `getFeeV2(provider, 10000)`:
   - `roundedGasLimit = 10000`, `10000 < 15000` → returns `feeInWei`. ✓
3. User B calls `getFeeV2(provider, 10001)`:
   - `roundedGasLimit = 20000`, `20000 > 15000` → additional fee triggered.
   - `additionalFee = (20000 − 15000) × feeInWei / 15000 = feeInWei / 3`
   - Returns `4/3 × feeInWei`. ✗ (user requested less than `defaultGasLimit`)
4. User B pays 33% more than User A despite requesting less gas than the provider's stated default limit, violating the base-fee guarantee. [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L775-793)
```text
        // Providers charge a minimum of their configured feeInWei for every request.
        // Requests using more than the defaultGasLimit get a proportionally scaled fee.
        // This approach may be somewhat simplistic, but it allows us to continue using the
        // existing feeInWei parameter for the callback failure flow instead of defining new
        // configuration values.
        uint32 roundedGasLimit = uint32(roundTo10kGas(gasLimit)) * TEN_THOUSAND;
        if (
            provider.defaultGasLimit > 0 &&
            roundedGasLimit > provider.defaultGasLimit
        ) {
            // This calculation rounds down the fee, which means that users can get some gas in the callback for free.
            // However, the value of the free gas is < 1 wei, which is insignificant.
            uint128 additionalFee = ((roundedGasLimit -
                provider.defaultGasLimit) * provider.feeInWei) /
                provider.defaultGasLimit;
            return provider.feeInWei + additionalFee;
        } else {
            return provider.feeInWei;
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L931-934)
```text
        roundTo10kGas(gasLimit);

        uint32 oldGasLimit = provider.defaultGasLimit;
        provider.defaultGasLimit = gasLimit;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L962-973)
```text
    function roundTo10kGas(uint32 gas) internal pure returns (uint16) {
        if (gas > MAX_GAS_LIMIT) {
            revert EntropyErrors.MaxGasLimitExceeded();
        }

        uint32 gas10k = gas / TEN_THOUSAND;
        if (gas10k * TEN_THOUSAND < gas) {
            gas10k += 1;
        }
        // Note: safe cast here should never revert due to the if statement above.
        return SafeCast.toUint16(gas10k);
    }
```
