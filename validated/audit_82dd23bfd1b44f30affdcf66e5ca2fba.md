### Title
Unauthenticated Governance Proposal Submission Endpoint Exposes Multisig Proposer Keypair - (File: `governance/xc_admin/packages/proposer_server/src/index.ts`)

### Summary
The `proposer_server` runs an Express HTTP server with a `POST /api/propose` endpoint that accepts arbitrary Solana `TransactionInstruction` objects and submits them as Squads multisig proposals using the server's loaded governance keypair. There is no authentication of any kind, and the server binds to all network interfaces by default.

### Finding Description
`governance/xc_admin/packages/proposer_server/src/index.ts` starts an Express server on `PORT` (default `4000`) with no authentication middleware. The only middleware applied is permissive CORS and body parsing: [1](#0-0) 

The single privileged route `POST /api/propose` deserializes caller-supplied instruction data — `programId`, `keys`, and `data` — directly into `TransactionInstruction` objects and passes them to `vault.proposeInstructions`, which signs and submits a Squads multisig proposal transaction using the server's `KEYPAIR`: [2](#0-1) 

The server is started with `app.listen(PORT)` — no host argument — which in Node.js defaults to binding on `0.0.0.0` (all interfaces): [3](#0-2) 

The `KEYPAIR` is the live governance proposer wallet loaded from disk at startup: [4](#0-3) 

The multisig target is `PRICE_FEED_MULTISIG`, the production Pyth price feed governance multisig: [5](#0-4) 

### Impact Explanation
Any network-reachable unauthenticated attacker can:

1. **Submit arbitrary governance proposals** to the Pyth price feed Squads multisig, signed by the server's keypair, targeting any `programId` with any instruction data — including proposals to transfer funds, change price feed configurations, or upgrade programs.
2. **Drain the server's SOL balance** through repeated proposal submissions, since each on-chain proposal transaction costs gas paid by `KEYPAIR`.
3. **Spam the multisig** with large volumes of malicious-looking proposals, disrupting legitimate governance operations and potentially tricking multisig signers into approving a malicious proposal.

While proposals still require multisig approval to execute, the attacker fully controls the *content* of what gets proposed under the server's authority, and directly depletes the proposer wallet's SOL.

### Likelihood Explanation
The server is intended to be reachable by the `xc_admin_frontend` (a web UI), meaning it is likely deployed in a network-accessible environment. With no authentication and binding to `0.0.0.0`, any host that can reach the server's port — including other processes on the same machine, internal network peers, or the public internet if not firewalled — can exploit this endpoint without any credentials.

### Recommendation
- Add authentication to `POST /api/propose` (e.g., a pre-shared API key checked via middleware, or mutual TLS).
- Bind the server to `127.0.0.1` by default: `app.listen(PORT, "127.0.0.1")`.
- Validate that submitted instructions target only expected program IDs before signing.

### Proof of Concept
```bash
curl -X POST http://<proposer-server-host>:4000/api/propose \
  -H "Content-Type: application/json" \
  -d '{
    "cluster": "mainnet-beta",
    "instructions": [{
      "programId": "<any_target_program>",
      "keys": [{"pubkey": "<attacker_pubkey>", "isSigner": false, "isWritable": true}],
      "data": [1,2,3,4]
    }]
  }'
```

This submits an attacker-crafted proposal to the `PRICE_FEED_MULTISIG` on mainnet, signed by the server's governance keypair, with no credentials required. [2](#0-1)

### Citations

**File:** governance/xc_admin/packages/proposer_server/src/index.ts (L23-25)
```typescript
const KEYPAIR: Keypair = Keypair.fromSecretKey(
  Uint8Array.from(JSON.parse(fs.readFileSync(envOrErr("WALLET"), "ascii"))),
);
```

**File:** governance/xc_admin/packages/proposer_server/src/index.ts (L47-51)
```typescript
const app = express();

app.use(cors());
app.use(express.json({ limit: "50mb" }));
app.use(express.urlencoded({ extended: true, limit: "50mb" }));
```

**File:** governance/xc_admin/packages/proposer_server/src/index.ts (L53-91)
```typescript
app.post("/api/propose", async (req: Request, res: Response) => {
  try {
    const instructions: TransactionInstruction[] = req.body.instructions.map(
      (ix: any) =>
        new TransactionInstruction({
          data: Buffer.from(ix.data),
          keys: ix.keys.map((key: any) => {
            return {
              isSigner: key.isSigner,
              isWritable: key.isWritable,
              pubkey: new PublicKey(key.pubkey),
            };
          }),
          programId: new PublicKey(ix.programId),
        }),
    );

    const cluster: PythCluster = req.body.cluster;

    const wallet = new NodeWallet(KEYPAIR);
    const proposeSquads: SquadsMesh = new SquadsMesh({
      connection: new Connection(RPC_URLS[getMultisigCluster(cluster)]),
      wallet,
    });

    const vault = new MultisigVault(
      wallet,
      getMultisigCluster(cluster),
      proposeSquads,
      PRICE_FEED_MULTISIG[getMultisigCluster(cluster)],
    );

    // preserve the existing API by returning only the first pubkey
    const proposalPubkey = (
      await vault.proposeInstructions(instructions, cluster, {
        computeUnitPriceMicroLamports: COMPUTE_UNIT_PRICE_MICROLAMPORTS!,
      })
    )[0];
    res.status(200).json({ proposalPubkey: proposalPubkey });
```

**File:** governance/xc_admin/packages/proposer_server/src/index.ts (L101-101)
```typescript
app.listen(PORT);
```
