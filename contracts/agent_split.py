# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
from dataclasses import dataclass
from genlayer import *

# ---------------------------------------------------------------- storage ---

BP = 10000          # basis points: shares must sum to exactly 10000 (= 100%)


@allow_storage
@dataclass
class Job:
    client: Address          # funds the budget, receives any refund
    coordinator: Address     # proposes the split and is paid its share
    title: str
    criteria: str            # what "done" means for this job
    budget: u256             # GEN held by the contract for this job
    total_bp: u256           # sum of the proposed shares (must reach 10000)
    alloc_count: u256
    state: u8                # 0=OPEN 1=REVIEWED_OK 2=REVIEWED_BAD 3=PAID 4=REFUNDED
    score: u256              # 0-100 fairness score from the jury
    verdict_reason: str
    confidence: str
    settled: bool


@allow_storage
@dataclass
class Allocation:
    agent: Address
    share_bp: u256           # basis points of the budget
    evidence_url: str        # what this agent delivered
    paid: bool


# external message interface for paying an EOA / EVM address on the chain layer
@gl.evm.contract_interface
class _Payee:
    class View:
        pass

    class Write:
        pass


# ------------------------------------------------------------------ contract --


class AgentSplit(gl.Contract):
    """Split one budget across several agents, with the fairness check done on-chain.

    Autonomous agents increasingly finish work as a group, and then a human has to decide who
    contributed what. AgentSplit moves that decision into the contract: the client escrows the
    budget, the coordinator proposes a split (agent, share in basis points, and the evidence that
    agent delivered), and an LLM jury fetches each piece of evidence and rules whether the shares
    actually match the contributions. A fair split pays out automatically; an unfair one returns
    the budget to the client instead of paying it out.

    Both directions are real transfers: the budget is custody (gl.message.value), every share leaves
    through emit_transfer, and a refund does too.
    """

    jobs: TreeMap[u256, Job]
    allocations: TreeMap[str, Allocation]    # key: "<job_id>:<index>"
    next_id: u256

    def __init__(self):
        self.next_id = u256(1)

    # -------------------------------------------------------- internals ----

    def _to_addr(self, value) -> Address:
        if isinstance(value, Address):
            return value
        return Address(value)

    def _addr_bytes(self, value) -> bytes:
        b = getattr(value, "as_bytes", None)
        if b is None:
            b = getattr(value, "_as_bytes", None)
        if callable(b):
            b = b()
        if isinstance(b, (bytes, bytearray)):
            return bytes(b)
        return bytes(self._to_addr(value))

    def _job(self, job_id: u256):
        if job_id not in self.jobs:
            raise gl.vm.UserError("Job not found")
        return self.jobs[job_id]

    def _alloc_key(self, job_id: u256, index: u256) -> str:
        return str(int(job_id)) + ":" + str(int(index))

    def _alloc(self, job_id: u256, index: u256):
        k = self._alloc_key(job_id, index)
        if k not in self.allocations:
            raise gl.vm.UserError("Allocation not found")
        return self.allocations[k]

    def _pay(self, to: Address, amount: u256):
        """Send GEN out of this contract (external message, executed on finalization).

        The explicit ``on='finalized'`` matches the documented form for value transfers;
        without it the message may not be emitted at all.
        """
        if amount <= u256(0):
            return
        _Payee(to).emit_transfer(value=u256(amount), on='finalized')

    # ------------------------------------------------------------- views ----

    @gl.public.view
    def get_job(self, job_id: u256) -> dict:
        j = self.jobs[job_id]
        return {
            "id": job_id,
            "client": "0x" + self._addr_bytes(j.client).hex(),
            "coordinator": "0x" + self._addr_bytes(j.coordinator).hex(),
            "title": j.title,
            "criteria": j.criteria,
            "budget": j.budget,
            "total_bp": j.total_bp,
            "alloc_count": j.alloc_count,
            "state": j.state,
            "score": j.score,
            "verdict_reason": j.verdict_reason,
            "confidence": j.confidence,
            "settled": j.settled,
            "contract_balance": self.balance,
        }

    @gl.public.view
    def get_allocation(self, job_id: u256, index: u256) -> dict:
        a = self._alloc(job_id, index)
        return {
            "agent": "0x" + self._addr_bytes(a.agent).hex(),
            "share_bp": a.share_bp,
            "evidence_url": a.evidence_url,
            "paid": a.paid,
        }

    @gl.public.view
    def get_job_count(self) -> u256:
        return self.next_id - u256(1)

    @gl.public.view
    def get_contract_balance(self) -> u256:
        return self.balance

    # ------------------------------------------------------------ writes ----

    @gl.public.write.payable
    def create_job(self, coordinator: Address, title: str, criteria: str) -> u256:
        """Client escrows the whole budget (payable)."""
        value = gl.message.value
        if value <= u256(0):
            raise gl.vm.UserError("Budget must be funded: send GEN with the call")
        if len(title.strip()) == 0:
            raise gl.vm.UserError("Title is required")
        if len(criteria.strip()) == 0:
            raise gl.vm.UserError("Completion criteria are required")

        client = self._to_addr(gl.message.sender_address)
        coord = self._to_addr(coordinator)
        if self._addr_bytes(client) == self._addr_bytes(coord):
            raise gl.vm.UserError("Client and coordinator must differ")

        j = Job(
            client=client,
            coordinator=coord,
            title=title,
            criteria=criteria,
            budget=value,
            total_bp=u256(0),
            alloc_count=u256(0),
            state=u8(0),
            score=u256(0),
            verdict_reason="",
            confidence="",
            settled=False,
        )
        jid = self.next_id
        self.jobs[jid] = j
        self.next_id = jid + u256(1)
        return jid

    @gl.public.write
    def add_allocation(self, job_id: u256, agent: Address, share_bp: u256, evidence_url: str):
        """Coordinator adds one agent's share and the evidence it delivered."""
        j = self._job(job_id)
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(j.coordinator):
            raise gl.vm.UserError("Only the coordinator can propose the split")
        if j.state != u8(0):
            raise gl.vm.UserError("Job is not open")
        if j.settled:
            raise gl.vm.UserError("Job already settled")
        if int(share_bp) <= 0:
            raise gl.vm.UserError("Share must be positive")
        if int(j.total_bp) + int(share_bp) > BP:
            raise gl.vm.UserError("Shares would exceed 100%")
        if len(evidence_url.strip()) == 0 or not (
                evidence_url.startswith("http://") or evidence_url.startswith("https://")):
            raise gl.vm.UserError("A valid evidence URL is required")

        idx = j.alloc_count
        self.allocations[self._alloc_key(job_id, idx)] = Allocation(
            agent=self._to_addr(agent),
            share_bp=share_bp,
            evidence_url=evidence_url,
            paid=False,
        )
        j.alloc_count = idx + u256(1)
        j.total_bp = j.total_bp + share_bp

    @gl.public.write
    def review_split(self, job_id: u256):
        """LLM jury fetches every agent's evidence and rules whether the shares are fair."""
        j = self._job(job_id)
        if j.state != u8(0):
            raise gl.vm.UserError("Split has already been reviewed")
        if int(j.alloc_count) == 0:
            raise gl.vm.UserError("No allocations proposed")
        if int(j.total_bp) != BP:
            raise gl.vm.UserError("Shares must sum to exactly 100%")

        # Snapshot into plain Python values before entering the nondet block.
        title_mem = str(j.title)
        crit_mem = str(j.criteria)
        budget_mem = int(j.budget)
        n = int(j.alloc_count)
        shares = []
        urls = []
        for i in range(n):
            a = self._alloc(job_id, u256(i))
            shares.append(int(a.share_bp))
            urls.append(str(a.evidence_url))

        def _fetch_text(url: str, limit: int = 700) -> str:
            try:
                resp = gl.nondet.web.get(url)
                if isinstance(resp, bytes):
                    raw = resp.decode("utf-8", errors="ignore")
                elif hasattr(resp, "body"):
                    raw = resp.body.decode("utf-8", errors="ignore")
                else:
                    raw = str(resp)
                return " ".join(raw.split())[:limit]
            except Exception:
                return "[could not fetch this evidence]"

        def leader_fn():
            lines = []
            for i in range(n):
                pct = shares[i] * 100 // BP
                body = _fetch_text(urls[i])
                lines.append(
                    "AGENT " + str(i + 1) + " — proposed share " + str(shares[i]) +
                    " bp (" + str(pct) + "% of the budget)\n"
                    "  evidence url: " + urls[i] + "\n"
                    "  evidence fetched now: " + body + "\n"
                )
            prompt = (
                "You are auditing how a group of autonomous agents agreed to split one budget.\n\n"
                "JOB: " + title_mem + "\n"
                "COMPLETION CRITERIA: " + crit_mem + "\n"
                "TOTAL BUDGET: " + str(budget_mem) + " wei of GEN\n\n"
                + "\n".join(lines) +
                "\nDecide ONLY from the fetched evidence above whether each proposed share is "
                "proportionate to what that agent actually delivered for THIS job. Approve the "
                "split when every share has real, job-relevant evidence behind it and no agent is "
                "paid for work that is missing or unrelated. Reject it when an agent's evidence is "
                "unreachable, empty, generic, or does not correspond to its share — e.g. an agent "
                "taking a large share with thin evidence.\n\n"
                'Respond as JSON with exactly these keys:\n'
                '- "fair": true or false\n'
                '- "score": integer 0-100 fairness score\n'
                '- "reason": concise 2-4 sentence justification citing the fetched evidence\n'
                '- "confidence": one of "high", "medium", "low"'
            )
            return gl.nondet.exec_prompt(prompt, response_format='json')

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            mine = leader_fn()
            if not isinstance(mine, dict) or not isinstance(leaders_res.calldata, dict):
                return False
            x = mine.get("fair")
            y = leaders_res.calldata.get("fair")
            if isinstance(x, str):
                x = x.strip().lower() == "true"
            if isinstance(y, str):
                y = y.strip().lower() == "true"
            return bool(x) == bool(y)

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        ok = result.get("fair", False)
        if isinstance(ok, str):
            ok = ok.strip().lower() == "true"

        score = result.get("score", 0)
        try:
            score_val = int(score)
        except Exception:
            score_val = 0
        if score_val < 0:
            score_val = 0
        if score_val > 100:
            score_val = 100

        j.state = u8(1) if bool(ok) else u8(2)
        j.score = u256(score_val)
        j.verdict_reason = str(result.get("reason", ""))
        j.confidence = str(result.get("confidence", ""))

    @gl.public.write
    def settle_split(self, job_id: u256):
        """Pay every agent its share. Only a fair split may be paid out."""
        j = self._job(job_id)
        if j.settled:
            raise gl.vm.UserError("Job already settled")
        if j.state != u8(1):
            raise gl.vm.UserError("Split is not approved as fair")
        if int(j.total_bp) != BP:
            raise gl.vm.UserError("Shares do not sum to 100%")

        j.settled = True
        j.state = u8(3)                      # PAID

        n = int(j.alloc_count)
        budget = int(j.budget)
        paid_total = 0
        for i in range(n):
            a = self._alloc(job_id, u256(i))
            amount = budget * int(a.share_bp) // BP
            paid_total += amount
            a.paid = True
            self._pay(a.agent, u256(amount))

        # rounding remainder goes to the coordinator, so the contract ends at zero
        rest = budget - paid_total
        if rest > 0:
            self._pay(j.coordinator, u256(rest))

    @gl.public.write
    def refund_job(self, job_id: u256):
        """Return the budget to the client when the split is not approved (or on demand while open)."""
        j = self._job(job_id)
        if j.settled:
            raise gl.vm.UserError("Job already settled")
        if j.state == u8(1):
            raise gl.vm.UserError("Split was approved; settle it instead")
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(j.client):
            raise gl.vm.UserError("Only the client can claim the refund")

        j.settled = True
        j.state = u8(4)                      # REFUNDED
        self._pay(j.client, j.budget)
