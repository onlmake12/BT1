### Title
Unchecked `providerToCredit` in `executeCallback` Allows Fee Theft After Exclusivity Period — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
The `executeCallback` function in `Echo.sol` accepts a caller-supplied `providerToCredit` address. After the exclusivity period elapses, there is no validation that `providerToCredit` equals `msg.sender` or the request's assigned provider. An unprivileged attacker who has pre-registered as a provider can call `executeCallback` with their own address as `providerToCredit`, crediting themselves with the fee that should have gone to the legitimate provider.

### Finding Description
When a user calls `requestPriceUpdatesWithCallback`, the request stores the assigned provider and the fee paid:

```solidity
req.provider = provider;
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [1](#0-0) 

When `executeCallback` is later called, the exclusivity check only enforces `providerToCredit == req.provider` **within** the exclusivity window:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
``` [2](#0-1) 

After the exclusivity period, `providerToCredit` is completely unchecked. There is no requirement that it equals `msg.sender`, the original `req.provider`, or even a registered provider. The fee stored in `req.fee` is then credited to `_state.providers[providerToCredit].accruedFeesInWei`, which the attacker can subsequently withdraw via `withdrawAsFeeManager`.

Provider registration is permissionless:

```solidity
function registerProvider(
    uint96 baseFeeInWei,
    uint96 feePerFeedInWei,
    uint96 feePerGasInWei
) external override {
    ProviderInfo storage provider = _state.providers[msg.sender];
    require(!provider.isRegistered, "Provider already registered");
``` [3](#0-2) 

The `withdrawAsFeeManager` path allows the attacker to drain the credited balance:

```solidity
require(
    msg.sender == _state.providers[provider].feeManager,
    "Only fee manager"
);
...
_state.providers[provider].accruedFeesInWei -= amount;
(bool sent, ) = msg.sender.call{value: amount}("");
``` [4](#0-3) 

The IEcho interface confirms `providerToCredit` is a free parameter with no on-chain constraint beyond the exclusivity window:

```solidity
function executeCallback(
    address providerToCredit,
    uint64 sequenceNumber,
    bytes[] calldata updateData,
    bytes32[] calldata priceIds
) external payable;
``` [5](#0-4) 

### Impact Explanation
Any fee paid by a requester into a pending Echo request can be redirected to an attacker-controlled address once the exclusivity period expires. The legitimate provider who was assigned the request receives nothing. Because price update data is publicly available from Hermes, the attacker does not need any privileged information to construct a valid `executeCallback` call. All in-flight requests whose exclusivity window has elapsed are simultaneously at risk.

### Likelihood Explanation
The attack requires only:
1. Registering as a provider (permissionless, zero-cost).
2. Monitoring on-chain requests (public state).
3. Fetching price update data from Hermes (public REST API).
4. Calling `executeCallback` with `providerToCredit = attackerAddress` after the exclusivity period.

No privileged role, leaked key, or governance majority is needed. The attacker-controlled entry path is entirely unprivileged.

### Recommendation
- **Short term:** Add a check inside `executeCallback` that, after the exclusivity period, requires `providerToCredit == msg.sender`. This ensures only the address that actually submits the fulfillment transaction can claim the fee.
- **Long term:** Consider storing the fulfilling caller's address on-chain at the time of fulfillment and validating it against `providerToCredit`, mirroring the two-step ownership-acceptance pattern used elsewhere in the codebase (e.g., `Ownable2Step` in `EntropyUpgradable`).

### Proof of Concept
1. Attacker calls `registerProvider(0, 0, 0)` — becomes a registered provider.
2. Attacker calls `setFeeManager(attackerAddress)` — sets themselves as their own fee manager.
3. Victim calls `requestPriceUpdatesWithCallback(legitimateProvider, publishTime, priceIds, gasLimit)` paying fee `F`.
4. Attacker waits for `block.timestamp >= publishTime + exclusivityPeriodSeconds`.
5. Attacker fetches valid `updateData` for `priceIds` from the public Hermes API.
6. Attacker calls `executeCallback(attackerAddress, sequenceNumber, updateData, priceIds)`.
   - Exclusivity check is skipped (period elapsed).
   - `providerToCredit = attackerAddress` is accepted without validation.
   - Fee `F` is credited to `_state.providers[attackerAddress].accruedFeesInWei`.
7. Attacker calls `withdrawAsFeeManager(attackerAddress, F)` — receives fee `F`.

The legitimate provider receives zero compensation for the request they were assigned.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L82-84)
```text
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L113-121)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L363-376)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L381-387)
```text
    function registerProvider(
        uint96 baseFeeInWei,
        uint96 feePerFeedInWei,
        uint96 feePerGasInWei
    ) external override {
        ProviderInfo storage provider = _state.providers[msg.sender];
        require(!provider.isRegistered, "Provider already registered");
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L70-75)
```text
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable;
```
