### Title
Publisher Self-Stake Rewards Permanently Lost on `set_publisher_stake_account` Switch — (`governance/pyth_staking_sdk/src/idl/integrity-pool.json`, `governance/pyth_staking_sdk/src/pyth-staking-client.ts`)

---

### Summary

When a publisher switches their registered self-staking account via `set_publisher_stake_account`, any unclaimed OIS rewards accumulated in the old account's `delegation_record` are permanently abandoned. The `advance_delegation_record` instruction is thereafter called only against the newly registered stake account, leaving the old delegation record's epoch gap unprocessable and the associated rewards unclaimable.

---

### Finding Description

The OIS integrity pool tracks per-delegator reward state in a `DelegationRecord` PDA whose seeds are `["delegation_record", publisher_pubkey, stake_account_positions]`. [1](#0-0) 

This means the delegation record is **address-bound**: it is unique to the combination of publisher key and the specific stake account public key. When a publisher calls `set_publisher_stake_account` to replace their registered self-staking account: [2](#0-1) 

…the `pool_data` account is updated to point to the new stake account. All subsequent calls to `advance_delegation_record` in the SDK read the registered stake account from `pool_data` and pass it as `publisherStakeAccountPositions`: [3](#0-2) 

The old delegation record — keyed to the old stake account address — is never advanced again. The epochs between the old record's `last_epoch` and the current epoch represent reward entitlements that are permanently unclaimable, because the on-chain program validates that `publisher_stake_account_positions` matches the currently registered account in `pool_data`.

The `DelegationRecord` type confirms it only stores `last_epoch` and `next_slash_event_index`; there is no migration path for accumulated epoch gaps: [4](#0-3) 

---

### Impact Explanation

A publisher who switches their self-staking account (e.g., for key rotation or account management) loses all OIS self-stake rewards earned since the last time `advance_delegation_record` was called on the old account. The `self_reward_ratio` values for those epochs are stored in pool events and cannot be retroactively claimed against the new account's delegation record, which starts fresh. [5](#0-4) 

**Note:** The reward rate `y` is currently set to 0 per OP-PIP-103, so no rewards are actively accruing. The vulnerability is structural and would become exploitable if/when rewards are re-enabled. [6](#0-5) 

---

### Likelihood Explanation

`set_publisher_stake_account` is a documented, UI-exposed operation available to any publisher: [7](#0-6) 

Publishers are expected to use this for legitimate account management. The loss is silent — no error is thrown, and the publisher receives no warning that unclaimed rewards will be forfeited. Likelihood is **medium** once rewards are re-enabled.

---

### Recommendation

Before executing `set_publisher_stake_account`, the protocol should require (or automatically trigger) a call to `advance_delegation_record` for the old stake account, settling all outstanding rewards up to the current epoch. Alternatively, the program should reject `set_publisher_stake_account` if the old delegation record has unprocessed epochs (`last_epoch < current_epoch`), forcing the publisher to claim first.

---

### Proof of Concept

1. Publisher P has self-staking account `old_account`. Rewards accrue over epochs E1–E5. `delegation_record(P, old_account).last_epoch = E1`.
2. Publisher P calls `set_publisher_stake_account(publisher=P, current=old_account, new=new_account)`. `pool_data.publisher_stake_accounts[P] = new_account`.
3. At epoch E6, anyone calls `advance_delegation_record` for publisher P. The SDK reads `stakeAccount = new_account` from `pool_data` and passes it as `publisherStakeAccountPositions`.
4. A new `delegation_record(P, new_account)` is initialized starting at E6. Rewards for E2–E5 against `delegation_record(P, old_account)` are permanently unclaimable. [8](#0-7) [9](#0-8)

### Citations

**File:** governance/pyth_staking_sdk/src/pdas.ts (L41-53)
```typescript
export const getDelegationRecordAddress = (
  stakeAccountPositions: PublicKey,
  publisher: PublicKey,
) => {
  return PublicKey.findProgramAddressSync(
    [
      Buffer.from("delegation_record"),
      publisher.toBuffer(),
      stakeAccountPositions.toBuffer(),
    ],
    INTEGRITY_POOL_PROGRAM_ADDRESS,
  )[0];
};
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L721-760)
```json
    {
      "accounts": [
        {
          "name": "signer",
          "signer": true
        },
        {
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "name": "publisher"
        },
        {
          "name": "pool_data",
          "relations": ["pool_config"],
          "writable": true
        },
        {
          "name": "pool_config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "new_stake_account_positions_option",
          "optional": true
        },
        {
          "name": "current_stake_account_positions_option",
          "optional": true
        }
      ],
      "args": [],
      "discriminator": [99, 46, 72, 132, 100, 235, 211, 117],
      "name": "set_publisher_stake_account"
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L684-713)
```typescript
  async getAdvanceDelegationRecordInstructions(
    stakeAccountPositions: PublicKey,
    payer?: PublicKey,
  ) {
    const poolData = await this.getPoolDataAccount();
    const stakeAccountPositionsData = await this.getStakeAccountPositions(
      stakeAccountPositions,
    );
    const allPublishers = extractPublisherData(poolData);
    const publishers = allPublishers
      .map((publisher) => {
        const positionsWithPublisher =
          stakeAccountPositionsData.data.positions.filter(
            ({ targetWithParameters }) =>
              targetWithParameters.integrityPool?.publisher.equals(
                publisher.pubkey,
              ),
          );

        let lowestEpoch;
        for (const position of positionsWithPublisher) {
          lowestEpoch = bigintMin(position.activationEpoch, lowestEpoch);
        }

        return {
          ...publisher,
          lowestEpoch,
        };
      })
      .filter(({ lowestEpoch }) => lowestEpoch !== undefined);
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L739-756)
```typescript
    const advanceDelegationRecordInstructions = await Promise.all(
      filteredPublishers.map(({ pubkey, stakeAccount }) =>
        this.integrityPoolProgram.methods
          .advanceDelegationRecord()
          .accountsPartial({
            payer: payer ?? this.wallet.publicKey,
            publisher: pubkey,
            publisherStakeAccountCustody: stakeAccount
              ? getStakeAccountCustodyAddress(stakeAccount)
              : null, // eslint-disable-line unicorn/no-null
            publisherStakeAccountPositions: stakeAccount,
            stakeAccountCustody: getStakeAccountCustodyAddress(
              stakeAccountPositions,
            ),
            stakeAccountPositions,
          })
          .instruction(),
      ),
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L827-844)
```typescript
  async setPublisherStakeAccount(
    publisher: PublicKey,
    stakeAccountPositions: PublicKey,
    newStakeAccountPositions: PublicKey | undefined,
  ) {
    const instruction = await this.integrityPoolProgram.methods
      .setPublisherStakeAccount()
      .accounts({
        currentStakeAccountPositionsOption: stakeAccountPositions,
        // eslint-disable-next-line unicorn/no-null
        newStakeAccountPositionsOption: newStakeAccountPositions ?? null,
        publisher,
      })
      .instruction();

    await sendTransaction([instruction], this.connection, this.wallet);
    return;
  }
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1475-1488)
```typescript
      name: "delegationRecord";
      type: {
        kind: "struct";
        fields: [
          {
            name: "lastEpoch";
            type: "u64";
          },
          {
            name: "nextSlashEventIndex";
            type: "u64";
          },
        ];
      };
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1787-1800)
```typescript
        fields: [
          {
            name: "selfRewardRatio";
            type: "u64";
          },
          {
            name: "otherRewardRatio";
            type: "u64";
          },
          {
            name: "delegationFee";
            type: "u64";
          },
        ];
```

**File:** apps/developer-hub/content/docs/oracle-integrity-staking/mathematical-representation.mdx (L9-11)
```text
<Callout type="info" title="OP-PIP-103">
  Per [OP-PIP-103](https://proposals.pyth.network/proposals/103), the reward rate $y$ is currently set to **0**. The staking and slashing mechanisms remain active.
</Callout>
```

**File:** apps/staking/src/api.ts (L409-420)
```typescript
export const reassignPublisherAccount = async (
  client: PythStakingClient,
  stakeAccount: PublicKey,
  targetAccount: PublicKey,
  publisherKey: PublicKey,
): Promise<void> => {
  return client.reassignPublisherStakeAccount(
    publisherKey,
    stakeAccount,
    targetAccount,
  );
};
```
