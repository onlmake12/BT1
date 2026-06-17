### Title
Linear Search Through `readerWhitelist` Array in `onlyWhitelistedReader` Modifier Causes Bounded Gas Overhead for On-Chain Readers - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary
The `onlyWhitelistedReader` modifier in `Scheduler.sol` performs an O(n) linear scan through the `readerWhitelist` address array on every protected read call. Although the array is capped at `MAX_READER_WHITELIST_SIZE = 255`, the pattern directly mirrors the reported vulnerability class: an access-control modifier that iterates an array instead of using a constant-time mapping lookup.

### Finding Description
The modifier `onlyWhitelistedReader` (lines 750–779) loads the `readerWhitelist` storage array and iterates through every element to check whether `msg.sender` is present:

```solidity
address[] storage whitelist = _state
    .subscriptionParams[subscriptionId]
    .readerWhitelist;
bool isWhitelisted = false;
for (uint i = 0; i < whitelist.length; i++) {
    if (whitelist[i] == msg.sender) {
        isWhitelisted = true;
        break;
    }
}
```

This modifier gates four externally callable functions: `getPricesUnsafe`, `getPricesNoOlderThan`, `getEmaPricesUnsafe`, and `getEmaPricesNoOlderThan`. Each call from an on-chain contract must pay for up to 255 cold `SLOAD` operations (one per whitelist slot) before the actual price-read logic executes.

The `readerWhitelist` is defined as a plain `address[]` in `SubscriptionParams`:

```solidity
address[] readerWhitelist; // Optional array of addresses allowed to read prices
```

The cap is enforced in `_validateSubscriptionParams`:

```solidity
if (params.readerWhitelist.length > MAX_READER_WHITELIST_SIZE) {
    revert SchedulerErrors.TooManyWhitelistedReaders(...);
}
```

where `MAX_READER_WHITELIST_SIZE = 255` (a `uint8`).

Additionally, `_validateSubscriptionParams` itself contains an O(n²) uniqueness check over the whitelist (lines 190–198), executed on every `createSubscription` and `updateSubscription` call, costing up to ~32,385 iterations at maximum size.

### Impact Explanation
Any on-chain contract that is a whitelisted reader and calls `getPricesUnsafe` / `getEmaPricesUnsafe` / etc. pays for up to 255 storage reads in the modifier before reaching the actual function body. At a full 255-entry whitelist, a non-matching caller (e.g., a contract that was removed from the whitelist but still calls the function) always pays the maximum scan cost and always reverts. This inflates gas costs for legitimate on-chain integrators and can make price reads economically unviable in high-gas environments when the whitelist is near capacity. The impact is bounded (not a full protocol halt) but is a concrete, measurable gas-cost DoS on whitelisted on-chain readers.

### Likelihood Explanation
Any subscription manager can set `readerWhitelist` to 255 entries via `createSubscription` or `updateSubscription`. This is an unprivileged, permissionless action. Once a subscription has a full whitelist, every on-chain read call against it incurs the maximum scan cost. The entry path requires no special role.

### Recommendation
Replace the `address[]` linear scan with a `mapping(address => bool)` per subscription, providing O(1) lookup:

```solidity
// In SchedulerState (or SubscriptionParams):
mapping(uint256 => mapping(address => bool)) public readerWhitelistMap;
```

The `onlyWhitelistedReader` modifier then becomes:

```solidity
modifier onlyWhitelistedReader(uint256 subscriptionId) {
    if (_state.subscriptionManager[subscriptionId] == msg.sender) { _; return; }
    if (!_state.subscriptionParams[subscriptionId].whitelistEnabled) { _; return; }
    if (!_state.readerWhitelistMap[subscriptionId][msg.sender])
        revert SchedulerErrors.Unauthorized();
    _;
}
```

The `address[]` array can be retained solely for enumeration (e.g., `getSubscription` return value) without being used in the hot access-control path.

### Proof of Concept

1. Deploy `Scheduler` (or use a live instance).
2. Call `createSubscription` with `whitelistEnabled = true` and `readerWhitelist` populated with 255 distinct addresses (none of which is the caller's address).
3. From a contract address **not** in the whitelist, call `getPricesUnsafe(subscriptionId, [])`.
4. Observe that the EVM executes 255 storage reads (`SLOAD`) inside `onlyWhitelistedReader` before reverting with `Unauthorized`. Gas consumed by the modifier alone is approximately `255 × 2100 gas (cold SLOAD) = ~535,500 gas`, paid entirely by the caller before the revert.
5. Even a **whitelisted** caller whose address is last in the array pays the full 255-slot scan cost in the worst case.

---

**Root cause references:**

`onlyWhitelistedReader` linear scan: [1](#0-0) 

`readerWhitelist` field definition: [2](#0-1) 

`MAX_READER_WHITELIST_SIZE = 255` cap: [3](#0-2) 

O(n²) uniqueness check in `_validateSubscriptionParams`: [4](#0-3) 

Functions gated by the modifier: [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L190-198)
```text
        for (uint i = 0; i < params.readerWhitelist.length; i++) {
            for (uint j = i + 1; j < params.readerWhitelist.length; j++) {
                if (params.readerWhitelist[i] == params.readerWhitelist[j]) {
                    revert SchedulerErrors.DuplicateWhitelistAddress(
                        params.readerWhitelist[i]
                    );
                }
            }
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L514-522)
```text
    function getPricesUnsafe(
        uint256 subscriptionId,
        bytes32[] calldata priceIds
    )
        external
        view
        override
        onlyWhitelistedReader(subscriptionId)
        returns (PythStructs.Price[] memory prices)
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L764-773)
```text
        address[] storage whitelist = _state
            .subscriptionParams[subscriptionId]
            .readerWhitelist;
        bool isWhitelisted = false;
        for (uint i = 0; i < whitelist.length; i++) {
            if (whitelist[i] == msg.sender) {
                isWhitelisted = true;
                break;
            }
        }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerStructs.sol (L11-11)
```text
        address[] readerWhitelist; // Optional array of addresses allowed to read prices
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L10-10)
```text
    uint8 public constant MAX_READER_WHITELIST_SIZE = 255;
```
