### Title
Unregistered `providerToCredit` in `Echo.executeCallback` Enables Fee Theft and Permanent Fee Locking — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.executeCallback` credits the request fee to an arbitrary caller-supplied `providerToCredit` address with no check that the address is a registered provider. After the 15-second exclusivity window, any attacker who has self-registered (permissionlessly) can call `executeCallback`, redirect the fee to themselves, and withdraw it — stealing fees from the legitimate provider. Alternatively, passing any unregistered address permanently locks the fee in the contract.

---

### Finding Description

`requestPriceUpdatesWithCallback` correctly gates on `isRegistered`:

```solidity
// Echo.sol line 58-61
require(
    _state.providers[provider].isRegistered,
    "Provider not registered"
);
``` [1](#0-0) 

`executeCallback` has no equivalent guard. After the exclusivity window, the only constraint is removed and the fee is credited unconditionally:

```solidity
// Echo.sol line 114-121 — exclusivity check (only active for 15 s)
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(providerToCredit == req.provider, "Only assigned provider during exclusivity period");
}
...
// Echo.sol line 161-162 — fee credited with no isRegistered check
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [2](#0-1) [3](#0-2) 

Provider registration is fully permissionless and free:

```solidity
// Echo.sol line 381-393
function registerProvider(uint96 baseFeeInWei, uint96 feePerFeedInWei, uint96 feePerGasInWei) external override {
    ProviderInfo storage provider = _state.providers[msg.sender];
    require(!provider.isRegistered, "Provider already registered");
    ...
    provider.isRegistered = true;
``` [4](#0-3) 

A registered provider can set themselves as their own fee manager:

```solidity
// Echo.sol line 350-358
function setFeeManager(address manager) external override {
    require(_state.providers[msg.sender].isRegistered, "Provider not registered");
    _state.providers[msg.sender].feeManager = manager;
``` [5](#0-4) 

And then withdraw via `withdrawAsFeeManager`:

```solidity
// Echo.sol line 360-379
function withdrawAsFeeManager(address provider, uint128 amount) external override {
    require(msg.sender == _state.providers[provider].feeManager, "Only fee manager");
    ...
    _state.providers[provider].accruedFeesInWei -= amount;
    (bool sent, ) = msg.sender.call{value: amount}("");
``` [6](#0-5) 

The `ProviderInfo` struct confirms `feeManager` and `isRegistered` are independent fields, so a self-registered attacker with `feeManager = self` satisfies all withdrawal conditions: [7](#0-6) 

---

### Impact Explanation

**Fee theft**: A registered attacker steals the full request fee (user-paid, intended for the legitimate provider) on every request whose exclusivity period has elapsed. The legitimate provider receives nothing.

**Permanent fee locking**: If `providerToCredit` is any unregistered address (e.g., `address(0xdead)`), `feeManager` is `address(0)` and there is no provider `withdraw()` function in `Echo.sol` — the credited wei is irrecoverable, permanently locked in the contract.

---

### Likelihood Explanation

- Provider registration is permissionless and costs only gas.
- The exclusivity period is 15 seconds by default.
- Valid `updateData` is publicly available from Hermes in real time.
- An attacker only needs to monitor the `PriceUpdateRequested` event, wait 15 seconds, fetch the Hermes update, and call `executeCallback`.
- No privileged access, no leaked keys, no oracle manipulation required.

---

### Recommendation

Add a registration guard at the top of `executeCallback`, mirroring the check already present in `requestPriceUpdatesWithCallback`:

```solidity
function executeCallback(
    address providerToCredit,
    uint64 sequenceNumber,
    bytes[] calldata updateData,
    bytes32[] calldata priceIds
) external payable override {
+   require(
+       _state.providers[providerToCredit].isRegistered,
+       "Provider not registered"
+   );
    Request storage req = findActiveRequest(sequenceNumber);
    ...
```

---

### Proof of Concept

```
1. Attacker calls echo.registerProvider(0, 0, 0)
   → _state.providers[attacker].isRegistered = true

2. Attacker calls echo.setFeeManager(attacker)
   → _state.providers[attacker].feeManager = attacker

3. Victim calls echo.requestPriceUpdatesWithCallback{value: fee}(
       legitimateProvider, publishTime, priceIds, gasLimit)
   → req.provider = legitimateProvider, req.fee = fee - pythFee

4. Attacker waits > exclusivityPeriodSeconds (15 s default)

5. Attacker fetches valid updateData from Hermes for priceIds

6. Attacker calls echo.executeCallback(
       attacker,          // providerToCredit — no isRegistered check
       sequenceNumber,
       updateData,
       priceIds)
   → _state.providers[attacker].accruedFeesInWei += (req.fee + msg.value - pythFee)
   → legitimateProvider receives 0

7. Attacker calls echo.withdrawAsFeeManager(attacker, stolenAmount)
   → ETH transferred to attacker
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L58-61)
```text
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L114-121)
```text
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
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

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L31-46)
```text
    struct ProviderInfo {
        // Slot 1: 12 + 12 + 8 = 32 bytes
        uint96 baseFeeInWei;
        uint96 feePerFeedInWei;
        // 8 bytes padding

        // Slot 2: 12 + 16 + 4 = 32 bytes
        uint96 feePerGasInWei;
        uint128 accruedFeesInWei;
        // 4 bytes padding

        // Slot 3: 20 + 1 + 11 = 32 bytes
        address feeManager;
        bool isRegistered;
        // 11 bytes padding
    }
```
