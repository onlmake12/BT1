### Title
`constructProviderCommitment` Unbounded Hash Loop with `maxNumHashes == 0` Default Enables Gas Exhaustion and Permanent User Fund Lock — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.sol`'s `constructProviderCommitment` executes an unbounded `while` loop proportional to `req.numHashes`. The guard that caps `numHashes` is gated on `maxNumHashes != 0`, but `register()` never initialises `maxNumHashes`, so it defaults to zero and the guard is permanently disabled. A provider (permissionlessly registered) who does not advance their commitment allows `numHashes` to grow with every new user request. Once `numHashes` exceeds the block gas limit threshold, every `revealWithCallback` / `reveal` call for that provider reverts out-of-gas, permanently locking all in-flight user funds.

---

### Finding Description

**Root cause — unbounded loop:**

`constructProviderCommitment` iterates exactly `numHashes` times, each iteration executing one `keccak256`:

```solidity
// Entropy.sol:987-996
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
``` [1](#0-0) 

**Root cause — guard disabled by default:**

`numHashes` is set at request time as `assignedSequenceNumber − currentCommitmentSequenceNumber`. The only protection is:

```solidity
// Entropy.sol:251-256
if (
    providerInfo.maxNumHashes != 0 &&
    req.numHashes > providerInfo.maxNumHashes
) {
    revert EntropyErrors.LastRevealedTooOld();
}
``` [2](#0-1) 

The short-circuit `maxNumHashes != 0` means the check is **entirely skipped** when `maxNumHashes == 0`. `register()` never writes `maxNumHashes`; Solidity zero-initialises it, so every freshly registered provider starts with the guard disabled. [3](#0-2) 

The Fortuna keeper's `sync_max_num_hashes` only sets a non-zero value when `chain_config.max_num_hashes` is explicitly configured; it passes `unwrap_or(0)` otherwise, leaving the guard disabled: [4](#0-3) 

**Call chain to the loop:**

`revealWithCallback` → `revealHelper` → `constructProviderCommitment(req.numHashes, ...)`: [5](#0-4) 

`revealWithCallback` is `public` and callable by anyone: [6](#0-5) 

**`numHashes` is `uint32`** (max ~4.3 billion), stored in the request struct: [7](#0-6) 

The test suite explicitly documents that `maxNumHashes == 0` disables the check: [8](#0-7) 

---

### Impact Explanation

Each `keccak256` costs ~30 gas. Ethereum's block gas limit is ~30 million gas. At that rate, ~1 million accumulated unrevealed requests are sufficient to make every `revealWithCallback` call for that provider revert out-of-gas. Because `revealWithCallback` is the only path to clear an in-flight request, all user funds paid into those requests are permanently locked in the contract with no recovery mechanism. The `numHashes` value is frozen at request creation time; even if the provider later calls `advanceProviderCommitment`, requests already stored with a large `numHashes` cannot be revealed.

---

### Likelihood Explanation

Provider registration is permissionless. `maxNumHashes` defaults to zero for every provider. A malicious provider deliberately omits `setMaxNumHashes`, accepts user requests (collecting fees), and withholds reveals until the gap is irrecoverable. Alternatively, a legitimate provider who does not configure `max_num_hashes` in Fortuna (`unwrap_or(0)`) reaches the same state through negligence. The Fortuna default of `0` for unconfigured chains means this is the out-of-the-box behaviour for any new provider deployment.

---

### Recommendation

1. **Enforce a non-zero `maxNumHashes` at registration time.** `register()` should require `maxNumHashes > 0` or accept it as a parameter and store it, so the guard is always active.
2. **Remove the `!= 0` short-circuit.** The guard `if (providerInfo.maxNumHashes != 0 && ...)` should be `if (req.numHashes > providerInfo.maxNumHashes)` with `maxNumHashes` always set to a safe value.
3. **Fortuna default.** `chain_config.max_num_hashes.unwrap_or(0)` should use a safe non-zero default (e.g., `unwrap_or(1000)`) matching the expected maximum concurrent in-flight requests.

---

### Proof of Concept

```
1. Attacker calls register(fee, commitment, metadata, chainLength, uri)
   → maxNumHashes initialises to 0 (Solidity default)
   → guard at Entropy.sol:251-256 is permanently disabled

2. N users each call requestV2(attackerProvider, gasLimit)
   → each request stores numHashes = assignedSeqNum - currentCommitmentSeqNum
   → numHashes grows by 1 per request; no cap enforced

3. After ~1,000,000 requests without any reveal:
   → any call to revealWithCallback(attackerProvider, seqN, userContrib, providerContrib)
   → enters revealHelper → constructProviderCommitment(~1_000_000, ...)
   → loops ~1,000,000 × keccak256 ≈ 30,000,000 gas → exceeds block gas limit → OOG revert

4. All N user fees are permanently locked; no clearRequest path is reachable.
```

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L247-256)
```text
        req.numHashes = SafeCast.toUint32(
            assignedSequenceNumber -
                providerInfo.currentCommitmentSequenceNumber
        );
        if (
            providerInfo.maxNumHashes != 0 &&
            req.numHashes > providerInfo.maxNumHashes
        ) {
            revert EntropyErrors.LastRevealedTooOld();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L395-403)
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

**File:** apps/fortuna/src/command/setup_provider.rs (L177-183)
```rust
    sync_max_num_hashes(
        &contract,
        &provider_info,
        chain_config.max_num_hashes.unwrap_or(0),
    )
    .in_current_span()
    .await?;
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStructsV2.sol (L44-50)
```text
    struct Request {
        // Storage slot 1 //
        address provider;
        uint64 sequenceNumber;
        // The number of hashes required to verify the provider revelation.
        uint32 numHashes;
        // Storage slot 2 //
```

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L1554-1568)
```text
    function testZeroMaxNumHashesDisableChecks() public {
        for (uint256 i = 0; i < provider1MaxNumHashes; i++) {
            request(user1, provider1, 42, false);
        }
        assertRequestReverts(
            random.getFee(provider1),
            provider1,
            42,
            false,
            EntropyErrors.LastRevealedTooOld.selector
        );
        vm.prank(provider1);
        random.setMaxNumHashes(0);
        request(user1, provider1, 42, false);
    }
```
