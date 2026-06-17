### Title
Unbounded Provider Fee in `setProviderFee` / `setProviderFeeAsFeeManager` Enables Effective DoS of Entropy Requests — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `setProviderFee`, `setProviderFeeAsFeeManager`, and `register` functions in `Entropy.sol` accept any `uint128` value for the provider fee with no upper-bound validation. A registered Entropy provider can set their `feeInWei` to `type(uint128).max`, causing `getFeeV2()` to overflow and revert (Solidity 0.8+ checked arithmetic), or simply making the fee unaffordable for any user. If the affected provider is the contract's default provider, every call to `requestV2()` (no-arg form) is DoS'd until the admin intervenes.

---

### Finding Description

`setProviderFee` (line 810) and `setProviderFeeAsFeeManager` (line 829) in `Entropy.sol` accept an arbitrary `uint128 newFeeInWei` with no upper-bound check. The only guard is that the caller is a registered provider:

```solidity
// Set provider fee. It will revert if provider is not registered.
function setProviderFee(uint128 newFeeInWei) external override {
    EntropyStructsV2.ProviderInfo storage provider = _state.providers[msg.sender];
    if (provider.sequenceNumber == 0) {
        revert EntropyErrors.NoSuchProvider();
    }
    uint128 oldFeeInWei = provider.feeInWei;
    provider.feeInWei = newFeeInWei;          // ← no upper-bound check
    ...
}
```

The same absence of bounds applies to `setProviderFeeAsFeeManager` (line 846) and to the initial `register` call (line 129).

Fee consumption happens in `getFeeV2`:

```solidity
function getFeeV2(address provider, uint32 gasLimit)
    public view override returns (uint128 feeAmount) {
    return getProviderFee(provider, gasLimit) + _state.pythFeeInWei;
}
```

If `provider.feeInWei == type(uint128).max` and `_state.pythFeeInWei > 0`, the addition overflows and reverts in Solidity 0.8+. Even if `pythFeeInWei == 0`, the fee is `type(uint128).max` wei — no user can supply that as `msg.value`, so every `request` / `requestV2` call for that provider reverts at the `InsufficientFee` check in `requestHelper` (line 235).

---

### Impact Explanation

- Any registered provider can set `feeInWei = type(uint128).max` in a single transaction.
- All users who call `requestV2()` (no-arg, uses the default provider) will have their transactions revert.
- Users who explicitly target that provider also cannot request randomness.
- The DoS persists until the admin changes the default provider or the provider lowers their fee — there is no on-chain enforcement preventing the fee from being set to an extreme value in the first place.
- No user funds are at risk of direct theft, but the liveness of the Entropy randomness service is broken for the affected provider's users.

---

### Likelihood Explanation

- Any permissionlessly registered Entropy provider can trigger this — no privileged key is required beyond being a registered provider.
- A typo (e.g., passing `type(uint128).max` instead of a reasonable wei amount) or a malicious provider acting in bad faith can cause this.
- The default provider is admin-controlled, so the admin can mitigate after the fact, but there is no on-chain prevention.

---

### Recommendation

Add an upper-bound constant (e.g., `MAX_PROVIDER_FEE_IN_WEI`) and enforce it in `setProviderFee`, `setProviderFeeAsFeeManager`, and `register`:

```solidity
uint128 public constant MAX_PROVIDER_FEE_IN_WEI = 1 ether; // example bound

function setProviderFee(uint128 newFeeInWei) external override {
    if (newFeeInWei > MAX_PROVIDER_FEE_IN_WEI)
        revert EntropyErrors.InvalidFee();
    ...
}
```

Similarly, `EntropyGovernance.setPythFee` should enforce an upper bound on the protocol fee to prevent the same overflow in `getFeeV2`.

---

### Proof of Concept

1. Register as a provider on the deployed `EntropyUpgradable` contract.
2. Call `setProviderFee(type(uint128).max)`.
3. If `_state.pythFeeInWei > 0`, call `getFeeV2(providerAddr, 0)` — it reverts with arithmetic overflow.
4. If `_state.pythFeeInWei == 0`, call `requestV2{value: 1 ether}(providerAddr, ...)` — it reverts with `InsufficientFee` because `msg.value < type(uint128).max`.
5. If this provider is the default provider, `requestV2()` (no-arg) is DoS'd for all users.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L111-129)
```text
    function register(
        uint128 feeInWei,
        bytes32 commitment,
        bytes calldata commitmentMetadata,
        uint64 chainLength,
        bytes calldata uri
    ) public override {
        if (chainLength == 0) revert EntropyErrors.AssertionFailure();

        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];

        // NOTE: this method implementation depends on the fact that ProviderInfo will be initialized to all-zero.
        // Specifically, accruedFeesInWei is intentionally not set. On initial registration, it will be zero,
        // then on future registrations, it will be unchanged. Similarly, provider.sequenceNumber defaults to 0
        // on initial registration.

        provider.feeInWei = feeInWei;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L760-765)
```text
    function getFeeV2(
        address provider,
        uint32 gasLimit
    ) public view override returns (uint128 feeAmount) {
        return getProviderFee(provider, gasLimit) + _state.pythFeeInWei;
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L810-827)
```text
    function setProviderFee(uint128 newFeeInWei) external override {
        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];

        if (provider.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }
        uint128 oldFeeInWei = provider.feeInWei;
        provider.feeInWei = newFeeInWei;
        emit ProviderFeeUpdated(msg.sender, oldFeeInWei, newFeeInWei);
        emit EntropyEventsV2.ProviderFeeUpdated(
            msg.sender,
            oldFeeInWei,
            newFeeInWei,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L829-855)
```text
    function setProviderFeeAsFeeManager(
        address provider,
        uint128 newFeeInWei
    ) external override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];

        if (providerInfo.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }

        if (providerInfo.feeManager != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }

        uint128 oldFeeInWei = providerInfo.feeInWei;
        providerInfo.feeInWei = newFeeInWei;

        emit ProviderFeeUpdated(provider, oldFeeInWei, newFeeInWei);
        emit EntropyEventsV2.ProviderFeeUpdated(
            provider,
            oldFeeInWei,
            newFeeInWei,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L67-74)
```text
    function setPythFee(uint128 newPythFee) external {
        _authoriseAdminAction();

        uint oldPythFee = _state.pythFeeInWei;
        _state.pythFeeInWei = newPythFee;

        emit PythFeeSet(oldPythFee, newPythFee);
    }
```
