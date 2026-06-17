Looking at the actual `allocRequest` and `requestKey` implementations to verify the claim precisely.

### Title
Attacker Can Force Sustained ~21k Gas Overhead Per Request by Pre-Filling All 32 `requests` Slots — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

An unprivileged attacker can register multiple providers and submit unrevealed requests that occupy all 32 slots of the `requests` array, one per `shortHash` index. Once all slots are occupied, every subsequent honest user request triggers `allocRequest` to evict a prior request into the `requestsOverflow` mapping — a cold SSTORE costing ~21k gas — paid by the honest requester, not the attacker.

---

### Finding Description

**`requestKey`** computes a 5-bit slot index:

```solidity
hash = keccak256(abi.encodePacked(provider, sequenceNumber));
shortHash = uint8(hash[0] & NUM_REQUESTS_MASK); // 0x1f → 32 possible values
``` [1](#0-0) 

**`allocRequest`** evicts the prior occupant to the overflow mapping when a slot is already active:

```solidity
if (isActive(req)) {
    (bytes32 reqKey, ) = requestKey(req.provider, req.sequenceNumber);
    _state.requestsOverflow[reqKey] = req;   // cold SSTORE ≈ 21k gas
}
``` [2](#0-1) 

The design comment explicitly acknowledges this cost is intentional but assumes rarity:

> "This operation is expensive, but should be rare." [3](#0-2) 

**`register()` is fully permissionless** — any address can register as a provider: [4](#0-3) 

**`prefillRequestStorage`** pre-fills the array with `sequenceNumber = 0` entries (inactive), which warms the `requests[i]` storage slots but does **not** warm any `requestsOverflow` mapping slots — those remain cold until first written: [5](#0-4) 

The `requestsOverflow` mapping is declared in `EntropyState.sol`: [6](#0-5) 

---

### Impact Explanation

Every honest user request that hits an occupied slot pays an extra ~21k gas (cold SSTORE from zero to non-zero for the `requestsOverflow` mapping entry). At 50 gwei gas price this is ~0.001 ETH per request. The attacker's setup cost is bounded (32 provider registrations + 32 requests × fee), while the ongoing impact is unbounded — every future request on any occupied slot incurs the overhead.

---

### Likelihood Explanation

The attack is fully permissionless. `register()` has no access control. The attacker can register 32 providers with different addresses and make one request per provider, then compute offline which `(provider, sequenceNumber)` pairs produce `shortHash` values covering all 32 indices (0–31). Alternatively, a single provider with ~130 sequential requests (coupon-collector bound) suffices. The attacker never needs to reveal, so the slots remain occupied indefinitely.

---

### Recommendation

1. **Increase `NUM_REQUESTS`** to reduce collision probability (e.g., 256 slots makes full coverage require 256 unrevealed requests).
2. **Cap unrevealed requests per provider** or add a per-address rate limit to raise the attacker's setup cost.
3. **Charge a higher fee for requests that trigger overflow** (detect in `allocRequest` and require additional `msg.value`), shifting the cost to the requester who causes the eviction rather than the honest user who arrives later.
4. **Pre-warm `requestsOverflow` slots** is not practical (unbounded key space), so option 1 or 3 is preferred.

---

### Proof of Concept

```solidity
// 1. Register 32 providers with addresses chosen so that
//    keccak256(abi.encodePacked(provider, 1))[0] & 0x1f covers {0..31}
for (uint i = 0; i < 32; i++) {
    vm.prank(attackerProviders[i]);
    entropy.register(0, commitment, "", 1000, "");
    vm.prank(user);
    entropy.request{value: fee}(attackerProviders[i], userCommitment, false);
    // do NOT reveal — slot[shortHash[i]] is now occupied
}

// 2. Measure gas for an honest request that hits any occupied slot
uint gasBefore = gasleft();
entropy.request{value: fee}(honestProvider, userCommitment, false);
uint gasAfter = gasleft();
uint delta = gasBefore - gasAfter - baselineCost;
assert(delta >= 21000); // eviction to requestsOverflow
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L93-103)
```text
        if (prefillRequestStorage) {
            // Write some data to every storage slot in the requests array such that new requests
            // use a more consistent amount of gas.
            // Note that these requests are not live because their sequenceNumber is 0.
            for (uint8 i = 0; i < NUM_REQUESTS; i++) {
                EntropyStructsV2.Request storage req = _state.requests[i];
                req.provider = address(1);
                req.blockNumber = 1234;
                req.commitment = hex"0123";
            }
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L111-145)
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

        provider.originalCommitment = commitment;
        provider.originalCommitmentSequenceNumber = provider.sequenceNumber;
        provider.currentCommitment = commitment;
        provider.currentCommitmentSequenceNumber = provider.sequenceNumber;
        provider.commitmentMetadata = commitmentMetadata;
        provider.endSequenceNumber = provider.sequenceNumber + chainLength;
        provider.uri = uri;

        provider.sequenceNumber += 1;

        emit EntropyEvents.Registered(
            EntropyStructConverter.toV1ProviderInfo(provider)
        );
        emit EntropyEventsV2.Registered(msg.sender, bytes(""));
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L977-983)
```text
    function requestKey(
        address provider,
        uint64 sequenceNumber
    ) internal pure returns (bytes32 hash, uint8 shortHash) {
        hash = keccak256(abi.encodePacked(provider, sequenceNumber));
        shortHash = uint8(hash[0] & NUM_REQUESTS_MASK);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L1055-1067)
```text
        if (isActive(req)) {
            // There's already a prior active request in the storage slot we want to use.
            // Overflow the prior request to the requestsOverflow mapping.
            // It is important that this code overflows the *prior* request to the mapping, and not the new request.
            // There is a chance that some requests never get revealed and remain active forever. We do not want such
            // requests to fill up all of the space in the array and cause all new requests to incur the higher gas cost
            // of the mapping.
            //
            // This operation is expensive, but should be rare. If overflow happens frequently, increase
            // the size of the requests array to support more concurrent active requests.
            (bytes32 reqKey, ) = requestKey(req.provider, req.sequenceNumber);
            _state.requestsOverflow[reqKey] = req;
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L34-34)
```text
        mapping(bytes32 => EntropyStructsV2.Request) requestsOverflow;
```
