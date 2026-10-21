# AgentSplit

**Split one budget across several agents, with the fairness check done on-chain — built on GenLayer Intelligent Contracts.**

Autonomous agents increasingly finish work as a group, and then a human has to decide who
contributed what. AgentSplit moves that decision into the contract, where the budget already is.

- **The client escrows the budget** (`create_job`, payable) and names a coordinator.
- **The coordinator proposes the split** (`add_allocation`) — each agent's share in basis points
  plus the URL that evidences what that agent delivered. Shares must sum to exactly 100%.
- **An on-chain jury rules whether the split is fair** (`review_split`) — the contract fetches
  *every* evidence URL inside the non-deterministic block and decides whether each share is
  proportionate to what that agent actually delivered.
- **A fair split pays out automatically** (`settle_split`) — every agent receives its share as a
  real transfer.
- **An unfair split pays nobody** (`refund_job`) — the client reclaims the whole budget.

Every path ends in a real transfer. No budget can be stranded, and no agent gets paid for work the
contract never read.

---

## Why this needs GenLayer

Splitting a pot across contributors is a judgement call, and judgement needs to read the actual
work. A classic contract can hold the budget but cannot open a single evidence URL, so the split
has to happen off-chain and the contract just executes whatever the keyholder says — which is
exactly the arrangement the agents were supposed to be free of.

GenLayer's non-deterministic execution (`gl.nondet.web.get` + `gl.nondet.exec_prompt` under
`gl.vm.run_nondet_unsafe` consensus) lets the contract read every contributor's evidence and reach
a fairness verdict validators agree on, while the budget custody and the payouts stay deterministic
and real.

---

## Contract

`contracts/agent_split.py` — deployed on **Bradbury**.

```
0xd20EdA91C828ceb26fB9B2e7785113e21A15c931
```

### Money flow

| Step | Call | What moves |
|---|---|---|
| 1 | `create_job(coordinator, title, criteria)` — **payable** | Client → contract custody, booked as the job's `budget` |
| 2 | `add_allocation(job_id, agent, share_bp, evidence_url)` | No funds move. Shares accumulate; must total 10000 bp |
| 3 | `review_split(job_id)` | No funds move. The jury fetches every evidence URL and returns `{fair, score, reason, confidence}` |
| 4a | `settle_split(job_id)` — state FAIR | Every agent is paid `budget × share_bp / 10000` via `emit_transfer(..., on='finalized')`; rounding dust goes to the coordinator |
| 4b | `refund_job(job_id)` — state UNFAIR | The whole budget returns to the client |
| — | `refund_job(job_id)` — state OPEN | The client can always pull the budget back before it is reviewed |

### Guarantees the contract enforces

- A budget is required, and client and coordinator must differ.
- Only the coordinator can propose shares, and only while the job is open.
- Shares must be positive and must never exceed 100%; a review requires exactly 100%.
- A job can only be reviewed once, and only settled once.
- `settle_split` refuses anything that is not marked FAIR — an unfair split can never pay out.
- `refund_job` refuses an approved split (settle it instead) and refuses an already-settled job.
- The jury's score is clamped to 0–100 before it is stored.

---

## Repository layout

```
contracts/agent_split.py          the Intelligent Contract
tests/direct/test_agent_split.py  20 direct-mode tests
index.html                        web UI (no build step)
genlayer.config.yaml              network configuration (Bradbury default)
```

---

## Run the tests

```bash
pip install genlayer-test
pytest tests/direct/ -v
```

```
20 passed
```

The suite covers budget custody, coordinator-only proposals, share validation (positive, ≤100%,
must total 100%), the one-shot review rule, the score clamp, payouts on a fair split, refusal to pay
an unfair one, the settle-once and refund rules, client-only refunds, and a full
fund → propose → review → pay lifecycle plus a refund lifecycle.

Note: the direct VM does not model the contract's own GEN balance, so the tests assert the
contract's internal accounting. The actual movement of GEN is verified end-to-end on Bradbury.

---

## Deploy

```bash
genlayer network set testnet-bradbury
genlayer deploy --contract contracts/agent_split.py
```

## Use it

```bash
# 1. client escrows the budget and names a coordinator (payable)
genlayer write <CONTRACT> create_job --args <coordinator> "Deliver three products" "All deployed and documented" --value 0.6

# 2. coordinator proposes each share (basis points) with its evidence
genlayer write <CONTRACT> add_allocation --args 1 <agentA> 5000 "https://example.com/a"
genlayer write <CONTRACT> add_allocation --args 1 <agentB> 3000 "https://example.com/b"
genlayer write <CONTRACT> add_allocation --args 1 <agentC> 2000 "https://example.com/c"

# 3. the jury checks fairness on-chain
genlayer write <CONTRACT> review_split --args 1

# 4. pay the split, or refund the client
genlayer write <CONTRACT> settle_split --args 1
genlayer write <CONTRACT> refund_job --args 1
```

Web UI: open `index.html` — no build step, no dependencies.

---

## Verified on Bradbury

The end-to-end run exercises real custody and real payouts on both paths: a fair three-way split is
paid out to every agent, and a deliberately unfair split is rejected by the jury so the client's
budget is refunded instead of paid. See `_提交材料.md` and the `as_audit.txt` / `as_evidence.txt`
fields for the transaction hashes produced by that run.

### Browser wallet path

The web UI is not a mock: a browser wallet signs the transaction and the contract state changes.

Transaction 0x992bf4457816d784ea68568d24aeaf9891394f045293434a1e96b83570839680

- from `0x57e4c6B7f3377D243d7249d144dbb798CCCc8deC` — the connected browser wallet, not a local script key
- method `create_job`, `value` 0.2 GEN, execution result `FINISHED_WITH_RETURN`
- `job_count` moved 3 -> 4 and the contract balance 0.9 -> 1.1 GEN, so the call really landed
- job #4 records the escrowed 0.2 GEN with the coordinator passed in the same call

The signing path in `index.html`:

- `createClient` is **not** given `window.ethereum` as the account. The connected address is supplied
  per call as a JSON-RPC account object `{address, type:"json-rpc"}`, and the signing provider is
  wrapped so the SDK cannot fall back to `window.ethereum`. With more than one extension installed
  that fallback answers `4100 unauthorised` and the wallet never signs at all.
- `pickProvider()` prefers OKX (`window.okxwallet`) and then MetaMask / Rabby / any injected provider,
  so the wallet the user actually connected with is the one that signs.
- `wallet_switchEthereumChain` runs before every write. `4902` triggers `wallet_addEthereumChain`, and
  `4001` / `-32002` / `4100` each map to an explicit message instead of a silent failure.
  `eth_chainId` is compared against `4221` before anything is signed.
- A transaction hash is never treated as success: the receipt is awaited to `ACCEPTED` / `FINALIZED`
  and the execution result is checked first.

---

## License

MIT
