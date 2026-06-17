### Title
Delegators Lose Accrued OIS Rewards When `slash` Is Executed Before `advance_delegation_record` - (File: `governance/pyth_staking_sdk/src/idl/integrity-pool.json`)

---

### Summary

The Oracle Integrity Staking (OIS) integrity pool program exposes a permissionless `slash` instruction that directly reduces a delegator's `stakeAccountCustody` token balance and advances `delegation_record.next_slash_event_index` without first settling the delegator's accrued rewards via `advance_delegation_record`. Any caller can invoke `slash` on a delegator's account at any time after a slash event is created. If the delegator has unclaimed rewards (i.e., `delegation_record.last_epoch` lags behind the slash event's epoch), those rewards are permanently lost because subsequent calls to `advance_delegation_record` will compute reward entitlements against the already-reduced post-slash balance.

---

### Finding Description

The integrity pool program has two distinct reward-accounting operations:

**1. `advance_delegation_record`** — settles per-epoch rewards from `delegation_record.last_epoch` up to the current epoch and transfers them to the delegator's custody account. It also processes pending slash events via `next_slash_event_index`.

**2. `slash`** — permissionless (requires only an arbitrary `signer`); applies a pre-created `slash_event` to a specific delegator's stake account. It:
- Calls the staking program's `slash_account` via CPI, which directly transfers tokens from `stakeAccountCustody` to `slashCustody`
- Advances `delegation_record.next_slash_event_index`
- Does **not** first call `advance_delegation_record` to settle pending rewards

The `delegationRecord` struct tracks two fields:
- `lastEpoch` — last epoch for which rewards were settled
- `nextSlashEventIndex` — next slash event to apply [1](#0-0) 

The `slash` instruction accounts include writable `stake_account_custody` and `slash_custody`, confirming that tokens are transferred out of the delegator's account immediately: [2](#0-1) 

The `slash` instruction requires only a generic `signer` — no privileged role is needed to call it after a slash event has been created: [3](#0-2) 

The `createSlashEvent` instruction (which creates the slash event record) requires `rewardProgramAuthority`: [4](#0-3) 

Once the slash event exists, the `slash` instruction is open to any caller. There is no on-chain enforcement requiring `advance_delegation_record` to be called before `slash`. The only ordering constraint enforced is sequential slash event index (`wrongSlashEventOrder`): [5](#0-4) 

The `advance_delegation_record` client-side logic confirms that reward settlement is a separate, independent operation from slashing: [6](#0-5) 

---

### Impact Explanation

A delegator who has not recently called `advance_delegation_record` (i.e., `delegation_record.last_epoch` < slash event epoch) will have their stake balance reduced by the `slash` instruction before their accrued rewards are settled. When `advance_delegation_record` is subsequently called, the reward calculation for epochs between `last_epoch` and the slash epoch will be computed against the post-slash (reduced) balance rather than the pre-slash balance. The rewards for those epochs are permanently lost and cannot be recovered. The slashed tokens are transferred to `slashCustody` and are not redistributed to the delegator.

This is a direct yield loss for OIS delegators — the same class of impact as the reference Concur finding.

---

### Likelihood Explanation

- Slash events are created by `rewardProgramAuthority` as part of the normal OIS slashing process (triggered by Pythian Council governance decisions).
- After a slash event is created, the `slash` instruction is permissionless — any transaction sender can call it on any delegator's account.
- Delegators who do not actively monitor and call `advance_delegation_record` every epoch (which is the common case — the UI shows a "Claim" button that users click manually) will have a non-zero gap between `last_epoch` and the current epoch.
- The gap is especially large for passive delegators who stake and forget, making this scenario realistic whenever a slashing event occurs.

---

### Recommendation

The `slash` instruction in the integrity pool program should enforce that `advance_delegation_record` has been called up to (at least) the slash event's epoch before applying the slash. Concretely:

1. Add a check in the `slash` instruction handler: require `delegation_record.last_epoch >= slash_event.epoch` before proceeding with the token transfer.
2. Alternatively, atomically settle rewards up to `slash_event.epoch` within the `slash` instruction itself before reducing the custody balance.
3. At minimum, document clearly that callers must invoke `advance_delegation_record` before `slash` to avoid reward loss, and enforce this ordering on-chain.

---

### Proof of Concept

1. Delegator Alice delegates to publisher P at epoch 1. Her `delegation_record.last_epoch = 1`, `next_slash_event_index = 0`.
2. Epochs 2–10 pass. Alice earns rewards but does not call `advance_delegation_record`. Her `last_epoch` remains 1.
3. At epoch 8, a data quality incident occurs. `rewardProgramAuthority` calls `create_slash_event` for publisher P with `index = 0`, `slash_ratio = 5%`, `epoch = 8`.
4. At epoch 10, Bob (any arbitrary signer) calls `slash(index=0)` on Alice's stake account. This:
   - Transfers 5% of Alice's staked tokens to `slashCustody`
   - Sets `delegation_record.next_slash_event_index = 1`
   - Does NOT settle Alice's rewards for epochs 2–9
5. Alice calls `advance_delegation_record`. The reward calculation for epochs 2–9 now uses Alice's post-slash balance (95% of original), not her pre-slash balance. Alice loses ~5% of the rewards she earned during epochs 2–7 (pre-slash epochs), which she had legitimately accrued before the incident. [7](#0-6) [8](#0-7)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1195-1208)
```json
      "name": "DelegationRecord",
      "type": {
        "fields": [
          {
            "name": "last_epoch",
            "type": "u64"
          },
          {
            "name": "next_slash_event_index",
            "type": "u64"
          }
        ],
        "kind": "struct"
      }
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L350-425)
```typescript
      name: "createSlashEvent";
      discriminator: [7, 214, 12, 127, 239, 247, 253, 117];
      accounts: [
        {
          name: "payer";
          writable: true;
          signer: true;
        },
        {
          name: "rewardProgramAuthority";
          signer: true;
          relations: ["poolConfig"];
        },
        {
          name: "slashCustody";
          relations: ["poolConfig"];
        },
        {
          name: "poolData";
          writable: true;
          relations: ["poolConfig"];
        },
        {
          name: "poolConfig";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103];
              },
            ];
          };
        },
        {
          name: "slashEvent";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [115, 108, 97, 115, 104, 95, 101, 118, 101, 110, 116];
              },
              {
                kind: "account";
                path: "publisher";
              },
              {
                kind: "arg";
                path: "index";
              },
            ];
          };
        },
        {
          name: "publisher";
          docs: [
            "CHECK : The publisher will be checked against data in the pool_data",
          ];
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
      ];
      args: [
        {
          name: "index";
          type: "u64";
        },
        {
          name: "slashRatio";
          type: "u64";
        },
      ];
    },
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L820-910)
```typescript
    },
    {
      name: "slash";
      discriminator: [204, 141, 18, 161, 8, 177, 92, 142];
      accounts: [
        {
          name: "signer";
          signer: true;
        },
        {
          name: "poolData";
          writable: true;
          relations: ["poolConfig"];
        },
        {
          name: "poolConfig";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103];
              },
            ];
          };
        },
        {
          name: "slashEvent";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [115, 108, 97, 115, 104, 95, 101, 118, 101, 110, 116];
              },
              {
                kind: "account";
                path: "publisher";
              },
              {
                kind: "arg";
                path: "index";
              },
            ];
          };
        },
        {
          name: "delegationRecord";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  100,
                  101,
                  108,
                  101,
                  103,
                  97,
                  116,
                  105,
                  111,
                  110,
                  95,
                  114,
                  101,
                  99,
                  111,
                  114,
                  100,
                ];
              },
              {
                kind: "account";
                path: "publisher";
              },
              {
                kind: "account";
                path: "stakeAccountPositions";
              },
            ];
          };
        },
        {
          name: "publisher";
          docs: [
            "CHECK : The publisher will be checked in the staking program",
          ];
        },
        {
          name: "stakeAccountPositions";
          writable: true;
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1446-1448)
```typescript
      name: "wrongSlashEventOrder";
      msg: "Slashes must be executed in order of slash event index";
    },
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1473-1488)
```typescript
  types: [
    {
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

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1804-1822)
```typescript
      name: "slashEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "epoch";
            type: "u64";
          },
          {
            name: "slashRatio";
            type: "u64";
          },
          {
            name: "slashCustody";
            type: "pubkey";
          },
        ];
      };
    },
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L779-792)
```typescript
  public async advanceDelegationRecord(stakeAccountPositions: PublicKey) {
    const instructions = await this.getAdvanceDelegationRecordInstructions(
      stakeAccountPositions,
    );

    return sendTransaction(
      [
        ...instructions.advanceDelegationRecordInstructions,
        ...instructions.mergePositionsInstruction,
      ],
      this.connection,
      this.wallet,
    );
  }
```
