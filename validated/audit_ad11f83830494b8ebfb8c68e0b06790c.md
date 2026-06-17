### Title
Attacker-Controlled `msg.sender` in On-Chain PRNG Enables User Contribution Manipulation - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `random()` internal function in `Entropy.sol` uses `msg.sender` as a direct entropy input to the on-chain PRNG seed. Because `msg.sender` is fully attacker-controlled via CREATE2 address grinding, an attacker who is also a registered provider can deterministically select their user contribution, compute the final random number off-chain, and only submit requests when the outcome is favorable.

---

### Finding Description

The `random()` function is called by the no-`userRandomNumber` variants of `requestV2()` to generate the user's contribution on-chain:

```solidity
function random() internal returns (bytes32) {
    _state.seed = keccak256(
        abi.encodePacked(
            block.timestamp,
            block.prevrandao,
            msg.sender,       // ← fully attacker-controlled
            _state.seed
        )
    );
    return _state.seed;
}
``` [1](#0-0) 

This output is passed directly as `userContribution` to `requestHelper`, which stores `constructUserCommitment(userContribution)` as part of the request commitment: [2](#0-1) [3](#0-2) 

The final random number is `combineRandomValues(userContribution, providerContribution, blockHash)`. The security of the 2-party protocol depends on the user's contribution being unknown to the provider at request time. If the user can choose their contribution, and they also know the provider's hash chain (because they are the provider), they can compute the final result before submitting the request.

**Attack path:**

1. Attacker registers as a provider (permissionless — `register()` is open to anyone).
2. Attacker reads `_state.seed` from public contract storage.
3. Attacker grinds CREATE2 salt values to find a deployer address `A` such that `keccak256(block.timestamp, block.prevrandao, A, _state.seed)` produces a user contribution that, when combined with the attacker's known provider hash chain value, yields a favorable final random number.
4. Attacker deploys a thin proxy at address `A` and calls `requestV2(attackerProvider, gasLimit)` from it.
5. Attacker fulfills the request via `revealWithCallback`, delivering the manipulated random number to the downstream application.

The `_state.seed` is public storage and all other inputs (`block.timestamp`, `block.prevrandao`) are observable before transaction inclusion, making the grinding fully deterministic. [4](#0-3) 

---

### Impact Explanation

Applications that call `requestV2()` (the no-`userRandomNumber` overloads) and use a provider that is the same entity as the requester — or a colluding provider — receive a fully attacker-chosen random number. This affects NFT mints, lotteries, games, and any protocol that relies on Pyth Entropy's convenience API for unbiased randomness. The attacker can guarantee any desired outcome with a bounded amount of CREATE2 grinding.

The `IEntropyV2` documentation acknowledges that "a dishonest validator and provider can collude to manipulate the result," but does not disclose that a user alone (without validator involvement) can control `msg.sender` via CREATE2 to achieve the same effect against a self-operated provider. [5](#0-4) 

---

### Likelihood Explanation

- Provider registration is **permissionless** — any address can call `register()`.
- `_state.seed` is **publicly readable** from contract storage.
- CREATE2 address grinding is **computationally cheap** (a few thousand hashes to find a favorable 32-byte output).
- `block.prevrandao` on Arbitrum (a primary Pyth Entropy deployment target) historically returns a value with limited entropy (or a near-constant), reducing the search space further.
- No privileged access, leaked keys, or external oracle manipulation is required.

---

### Recommendation

Remove `msg.sender` from the PRNG seed inputs, or replace it with a value that is not attacker-selectable (e.g., a monotonically incrementing nonce). Better yet, require callers of the no-`userRandomNumber` `requestV2()` variants to supply their own off-chain random commitment, and deprecate the on-chain PRNG path entirely. The `requestV2(address provider, bytes32 userRandomNumber, uint32 gasLimit)` overload already provides the correct interface. [6](#0-5) 

---

### Proof of Concept

```solidity
// Attacker is a registered provider with known hash chain `providerChain[]`.
// Target application calls requestV2() (no userRandomNumber).

// Step 1: Read current seed off-chain
bytes32 currentSeed = IEntropyState(entropyAddr).getSeed();

// Step 2: Grind CREATE2 salt until desired outcome
for (uint256 salt = 0; ; salt++) {
    address candidate = computeCreate2Address(salt);
    bytes32 userContrib = keccak256(abi.encodePacked(
        block.timestamp,
        block.prevrandao,
        candidate,
        currentSeed
    ));
    bytes32 finalRandom = keccak256(abi.encodePacked(
        userContrib,
        providerChain[nextSeqNum]
    ));
    if (finalRandom == desiredOutcome) {
        // Step 3: Deploy at candidate address and call requestV2
        deployAndRequest(salt, attackerProvider);
        break;
    }
}
// Step 4: Fulfill via revealWithCallback → downstream app receives desiredOutcome
```

The `random()` function's inclusion of `msg.sender` as an entropy source is the necessary vulnerable step: without it, the attacker cannot steer the user contribution without validator collusion. [1](#0-0)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L257-259)
```text
        req.commitment = keccak256(
            bytes.concat(userCommitment, providerInfo.currentCommitment)
        );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L286-310)
```text
    function requestV2()
        external
        payable
        override
        returns (uint64 assignedSequenceNumber)
    {
        assignedSequenceNumber = requestV2(getDefaultProvider(), random(), 0);
    }

    function requestV2(
        uint32 gasLimit
    ) external payable override returns (uint64 assignedSequenceNumber) {
        assignedSequenceNumber = requestV2(
            getDefaultProvider(),
            random(),
            gasLimit
        );
    }

    function requestV2(
        address provider,
        uint32 gasLimit
    ) external payable override returns (uint64 assignedSequenceNumber) {
        assignedSequenceNumber = requestV2(provider, random(), gasLimit);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L1079-1089)
```text
    function random() internal returns (bytes32) {
        _state.seed = keccak256(
            abi.encodePacked(
                block.timestamp,
                block.prevrandao,
                msg.sender,
                _state.seed
            )
        );
        return _state.seed;
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L46-50)
```text
    /// Note that this method uses an in-contract PRNG to generate the user's contribution to the random number.
    /// This approach modifies the security guarantees such that a dishonest validator and provider can
    /// collude to manipulate the result (as opposed to a malicious user and provider). That is, the user
    /// now trusts the validator honestly draw a random number. If you wish to avoid this trust assumption,
    /// call a variant of `requestV2` that accepts a `userRandomNumber` parameter.
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L97-101)
```text
    function requestV2(
        address provider,
        bytes32 userRandomNumber,
        uint32 gasLimit
    ) external payable returns (uint64 assignedSequenceNumber);
```
