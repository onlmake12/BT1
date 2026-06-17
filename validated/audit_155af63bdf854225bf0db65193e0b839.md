### Title
Unbounded `numHashes` Loop in `constructProviderCommitment` Can Exceed Block Gas Limit, Permanently Bricking Entropy Requests — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.sol`'s `constructProviderCommitment` function contains an unbounded `while` loop that iterates `numHashes` times. The `numHashes` value stored per-request equals `assignedSequenceNumber − currentCommitmentSequenceNumber` at request time, and is only guarded by the optional `maxNumHashes` field (which defaults to `0`, meaning no limit). An unprivileged user can make many requests against a provider that has not set `maxNumHashes`, driving `numHashes` to a value large enough that the subsequent `revealWithCallback` / `reveal` call exceeds the block gas limit, permanently preventing those requests from being fulfilled.

---

### Finding Description

**Root cause — `constructProviderCommitment`:**

```solidity
// Entropy.sol L987-996
function constructProviderCommitment(
    uint64 numHashes,
    bytes32 revelation
) internal pure returns (bytes32 currentHash) {
    currentHash = revelation;
    while (numHashes > 0) {                              // ← unbounded loop
        currentHash = keccak256(bytes.concat(currentHash));
        numHashes -= 1;
    }
}
``` [1](#0-0) 

This function is called inside `revealHelper` with the per-request `numHashes` field:

```solidity
// Entropy.sol L400-403
bytes32 providerCommitment = constructProviderCommitment(
    req.numHashes,
    providerContribution
);
``` [2](#0-1) 

`req.numHashes` is written at request time as:

```solidity
// Entropy.sol L247-250
req.numHashes = SafeCast.toUint32(
    assignedSequenceNumber -
        providerInfo.currentCommitmentSequenceNumber
);
``` [3](#0-2) 

The only guard is:

```solidity
// Entropy.sol L251-256
if (
    providerInfo.maxNumHashes != 0 &&
    req.numHashes > providerInfo.maxNumHashes
) {
    revert EntropyErrors.LastRevealedTooOld();
}
``` [4](#0-3) 

`maxNumHashes` is a storage field that defaults to `0`. When it is `0`, the condition short-circuits and **no upper bound is enforced on `numHashes`**.

**Attack path:**

1. Attacker identifies a provider whose `maxNumHashes == 0` (the default for any provider that has not explicitly called `setMaxNumHashes`).
2. Attacker calls `requestV2()` (or `requestWithCallback()`) many times in succession, paying the required fee each time. Each call increments `providerInfo.sequenceNumber` by 1 while `currentCommitmentSequenceNumber` remains unchanged (the provider has not yet revealed any request).
3. After `N` requests, the attacker's latest request stores `numHashes = N`.
4. When the Fortuna keeper (or anyone) calls `revealWithCallback` for that request, `revealHelper` → `constructProviderCommitment` must execute `N` `keccak256` iterations.
5. Each `keccak256(bytes.concat(...))` costs ≈ 36 gas (30 base + 6 for 32-byte input). At `N ≈ 700 000`, gas consumption reaches ≈ 25 M gas — the per-transaction limit on Base and many other chains.
6. The transaction reverts with out-of-gas. The request is permanently stuck; no recovery path exists for it.

`revealHelper` is called by both `reveal` and `revealWithCallback`, so both fulfillment paths are affected. [5](#0-4) 

---

### Impact Explanation

- Any in-flight entropy request whose stored `numHashes` exceeds the gas budget of a single transaction can **never be fulfilled** — neither by the Fortuna keeper nor by any other caller.
- This is a permanent liveness failure for those requests: the `callbackStatus` remains `CALLBACK_NOT_STARTED` forever (or the non-callback request is simply unrevealable).
- Users who paid fees receive no randomness and have no on-chain recourse.
- The impact is identical in class to the Megapot H-02: an expensive loop inside the entropy fulfillment path causes the transaction to exceed the block gas limit, preventing settlement.

---

### Likelihood Explanation

- `maxNumHashes` defaults to `0` for every newly registered provider. Any provider that does not explicitly call `setMaxNumHashes` is vulnerable.
- The attack requires only the ability to call `requestV2()` repeatedly while paying the provider's fee — a fully permissionless action available to any EOA or contract.
- The attacker's cost is `N × providerFee`. For providers with low fees (or zero fees during promotional periods), the cost to reach the gas-limit threshold is low.
- The Fortuna keeper's `advanceProviderCommitment` can partially mitigate this for *future* requests, but it cannot retroactively reduce `numHashes` already stored in existing requests.

---

### Recommendation

1. **Enforce a non-zero `maxNumHashes` for all providers.** In `requestHelper`, revert if `maxNumHashes == 0` and `numHashes` exceeds a protocol-level hard cap (e.g., 10 000 hashes ≈ 360 000 gas).
2. **Add a hard cap inside `constructProviderCommitment`** itself:
   ```solidity
   uint64 constant MAX_HASHES = 10_000;
   require(numHashes <= MAX_HASHES, "numHashes exceeds safe limit");
   ```
3. **Require providers to set `maxNumHashes` before accepting requests**, or default `maxNumHashes` to a safe value (e.g., 1 000) instead of `0`.

---

### Proof of Concept

```solidity
// Pseudocode — no privileged access required
IEntropyV2 entropy = IEntropyV2(ENTROPY_ADDRESS);
address provider = entropy.getDefaultProvider();   // maxNumHashes == 0 by default
uint128 fee = entropy.getFeeV2(provider, 0);

// Make 700 000 requests; each increments sequenceNumber by 1
// while currentCommitmentSequenceNumber stays at its initial value.
for (uint i = 0; i < 700_000; i++) {
    entropy.requestV2{value: fee}(provider, bytes32(i), 0);
}

// The last request has numHashes ≈ 700 000.
// revealWithCallback for that request will loop 700 000 times in
// constructProviderCommitment, consuming ≈ 25 M gas and reverting OOG.
// The request is permanently unresolvable.
```

The call chain is:

```
revealWithCallback(provider, seqNum, userContrib, providerContrib)
  └─ revealHelper(req, ...)
       └─ constructProviderCommitment(req.numHashes=700000, providerContrib)
            └─ while (numHashes > 0) { keccak256(...); }   // OOG after ~700k iterations
``` [6](#0-5) [1](#0-0)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L247-250)
```text
        req.numHashes = SafeCast.toUint32(
            assignedSequenceNumber -
                providerInfo.currentCommitmentSequenceNumber
        );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L251-256)
```text
        if (
            providerInfo.maxNumHashes != 0 &&
            req.numHashes > providerInfo.maxNumHashes
        ) {
            revert EntropyErrors.LastRevealedTooOld();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L395-439)
```text
    function revealHelper(
        EntropyStructsV2.Request storage req,
        bytes32 userContribution,
        bytes32 providerContribution
    ) internal returns (bytes32 randomNumber, bytes32 blockHash) {
        bytes32 providerCommitment = constructProviderCommitment(
            req.numHashes,
            providerContribution
        );
        bytes32 userCommitment = constructUserCommitment(userContribution);
        if (
            keccak256(bytes.concat(userCommitment, providerCommitment)) !=
            req.commitment
        ) revert EntropyErrors.IncorrectRevelation();

        blockHash = bytes32(uint256(0));
        if (req.useBlockhash) {
            bytes32 _blockHash = blockhash(req.blockNumber);

            // The `blockhash` function will return zero if the req.blockNumber is equal to the current
            // block number, or if it is not within the 256 most recent blocks. This allows the user to
            // select between two random numbers by executing the reveal function in the same block as the
            // request, or after 256 blocks. This gives each user two chances to get a favorable result on
            // each request.
            // Revert this transaction for when the blockHash is 0;
            if (_blockHash == bytes32(uint256(0)))
                revert EntropyErrors.BlockhashUnavailable();

            blockHash = _blockHash;
        }

        randomNumber = combineRandomValues(
            userContribution,
            providerContribution,
            blockHash
        );

        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            req.provider
        ];
        if (providerInfo.currentCommitmentSequenceNumber < req.sequenceNumber) {
            providerInfo.currentCommitmentSequenceNumber = req.sequenceNumber;
            providerInfo.currentCommitment = providerContribution;
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L542-566)
```text
    function revealWithCallback(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override {
        EntropyStructsV2.Request storage req = findActiveRequest(
            provider,
            sequenceNumber
        );

        if (
            !(req.callbackStatus ==
                EntropyStatusConstants.CALLBACK_NOT_STARTED ||
                req.callbackStatus == EntropyStatusConstants.CALLBACK_FAILED)
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }

        bytes32 randomNumber;
        (randomNumber, ) = revealHelper(
            req,
            userContribution,
            providerContribution
        );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L987-996)
```text
    function constructProviderCommitment(
        uint64 numHashes,
        bytes32 revelation
    ) internal pure returns (bytes32 currentHash) {
        currentHash = revelation;
        while (numHashes > 0) {
            currentHash = keccak256(bytes.concat(currentHash));
            numHashes -= 1;
        }
    }
```
