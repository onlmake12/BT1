### Title
Attacker-Controlled `providerToCredit` Parameter Enables Fee Theft via Front-Running in `Echo.executeCallback` — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.executeCallback` accepts an attacker-controlled `providerToCredit` parameter and credits the request fee to that address without verifying that `msg.sender == providerToCredit`. After the exclusivity period expires, any caller can front-run a legitimate provider's fulfillment transaction, copy the valid `updateData` and `priceIds`, and redirect the entire request fee to an address they control.

---

### Finding Description

`Echo.executeCallback` is the permissionless fulfillment entry point for price-update requests. Its only caller restriction is an exclusivity window:

```solidity
function executeCallback(
    address providerToCredit,
    uint64 sequenceNumber,
    bytes[] calldata updateData,
    bytes32[] calldata priceIds
) external payable override {
    Request storage req = findActiveRequest(sequenceNumber);

    // Check provider exclusivity using configurable period
    if (
        block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
    ) {
        require(
            providerToCredit == req.provider,
            "Only assigned provider during exclusivity period"
        );
    }
    ...
    _state.providers[providerToCredit].accruedFeesInWei += SafeCast
        .toUint128((req.fee + msg.value) - pythFee);
``` [1](#0-0) [2](#0-1) 

Once `block.timestamp >= req.publishTime + exclusivityPeriodSeconds`, the exclusivity `require` is skipped entirely. There is **no subsequent check** that `msg.sender == providerToCredit`. The fee accounting line unconditionally credits `_state.providers[providerToCredit].accruedFeesInWei` with the full provider fee (`req.fee + msg.value - pythFee`), where `providerToCredit` is a free parameter chosen by the caller.

Provider registration is permissionless:

```solidity
function registerProvider(...) external override {
    ProviderInfo storage provider = _state.providers[msg.sender];
    require(!provider.isRegistered, "Provider already registered");
    ...
}
``` [3](#0-2) 

An attacker pre-registers as a provider, then monitors the mempool for a legitimate provider's `executeCallback` transaction. They copy the `updateData` and `priceIds` payload, substitute `providerToCredit = attacker_address`, and submit with higher gas. The fee is credited to the attacker; the legitimate provider receives nothing.

---

### Impact Explanation

The legitimate provider who prepared and submitted the price-update data loses their entire accrued fee for that request. The attacker, who performed no off-chain work, receives the fee. At scale (many requests), this drains provider revenue and disincentivises honest participation in the Echo network. The stolen funds are real ETH locked in `accruedFeesInWei` and withdrawable by the attacker via `withdrawAsFeeManager` (after setting themselves as their own fee manager) or any provider withdrawal path.

---

### Likelihood Explanation

- The exclusivity period is configurable and currently defaults to 15 seconds. Any request older than 15 seconds past its `publishTime` is vulnerable.
- Front-running is a standard MEV technique on all EVM chains where Echo is deployed.
- The attacker only needs to copy calldata from the mempool — no cryptographic material or privileged access is required.
- Provider registration is permissionless, so the attacker can pre-register at zero cost.

---

### Recommendation

Add a check that the caller is the address being credited:

```solidity
require(
    msg.sender == providerToCredit,
    "Caller must be the credited provider"
);
```

This mirrors the pattern used in `setFeeManager` and `setProviderFee`, which correctly bind actions to `msg.sender` rather than a caller-supplied address. [4](#0-3) [5](#0-4) 

---

### Proof of Concept

1. Attacker calls `echo.registerProvider(0, 0, 0)` — registers with zero fees (permissionless).
2. A legitimate user calls `echo.requestPriceUpdatesWithCallback{value: fee}(legitimateProvider, publishTime, priceIds, gasLimit)`.
3. After `publishTime + exclusivityPeriodSeconds` elapses, `legitimateProvider` broadcasts `echo.executeCallback(legitimateProvider, seqNum, updateData, priceIds)`.
4. Attacker observes the pending transaction in the mempool, copies `updateData` and `priceIds`, and submits `echo.executeCallback(attackerAddress, seqNum, updateData, priceIds)` with higher gas.
5. Attacker's transaction is mined first. `_state.providers[attackerAddress].accruedFeesInWei` is incremented by `req.fee - pythFee`.
6. Legitimate provider's transaction reverts (`NoSuchRequest`) because the request was already cleared.
7. Attacker calls `echo.setFeeManager(attackerAddress)` (sets self as own fee manager) then `echo.withdrawAsFeeManager(attackerAddress, stolenAmount)` to extract the ETH. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-121)
```text
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable override {
        Request storage req = findActiveRequest(sequenceNumber);

        // Check provider exclusivity using configurable period
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-164)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L350-358)
```text
    function setFeeManager(address manager) external override {
        require(
            _state.providers[msg.sender].isRegistered,
            "Provider not registered"
        );
        address oldFeeManager = _state.providers[msg.sender].feeManager;
        _state.providers[msg.sender].feeManager = manager;
        emit FeeManagerUpdated(msg.sender, oldFeeManager, manager);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L360-379)
```text
    function withdrawAsFeeManager(
        address provider,
        uint128 amount
    ) external override {
        require(
            msg.sender == _state.providers[provider].feeManager,
            "Only fee manager"
        );
        require(
            _state.providers[provider].accruedFeesInWei >= amount,
            "Insufficient balance"
        );

        _state.providers[provider].accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L381-393)
```text
    function registerProvider(
        uint96 baseFeeInWei,
        uint96 feePerFeedInWei,
        uint96 feePerGasInWei
    ) external override {
        ProviderInfo storage provider = _state.providers[msg.sender];
        require(!provider.isRegistered, "Provider already registered");
        provider.baseFeeInWei = baseFeeInWei;
        provider.feePerFeedInWei = feePerFeedInWei;
        provider.feePerGasInWei = feePerGasInWei;
        provider.isRegistered = true;
        emit ProviderRegistered(msg.sender, feePerGasInWei);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L395-409)
```text
    function setProviderFee(
        address provider,
        uint96 newBaseFeeInWei,
        uint96 newFeePerFeedInWei,
        uint96 newFeePerGasInWei
    ) external override {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );
        require(
            msg.sender == provider ||
                msg.sender == _state.providers[provider].feeManager,
            "Only provider or fee manager can invoke this method"
        );
```
