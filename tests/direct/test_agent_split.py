# -*- coding: utf-8 -*-
"""AgentSplit direct-mode tests — budget split across agents, fairness checked on-chain.

Run: pytest tests/direct/ -v

Note: the direct VM does not model the contract's own GEN balance, so these tests assert the
contract's internal accounting (budget / shares / paid flags). The real movement of GEN is
verified end-to-end on Bradbury.
"""

BP = 10000          # 100% in basis points
BUDGET = 1000       # small numbers are fine: create_job has no minimum


def _deploy(direct_vm, direct_deploy):
    import pathlib
    import tempfile

    src = pathlib.Path(__file__).resolve().parents[2] / "contracts" / "agent_split.py"
    code = src.read_text(encoding="utf-8")
    tmp = pathlib.Path(tempfile.gettempdir()) / "agent_split_local.py"
    tmp.write_text(code, encoding="utf-8")
    return direct_deploy(str(tmp))


def _addr(v):
    from genlayer import Address
    return Address(v)


def _job(direct_vm, c, client, coord, budget=BUDGET,
         title="Ship the dashboard", criteria="Working dashboard deployed and documented"):
    direct_vm.sender = client
    direct_vm.value = budget
    jid = c.create_job(_addr(coord), title, criteria)
    direct_vm.value = 0
    return jid


def _alloc(direct_vm, c, coord, jid, agent, share_bp, url="https://example.com/work"):
    direct_vm.sender = coord
    c.add_allocation(jid, _addr(agent), share_bp, url)


# ------------------------------------------------- 1. job creation ----

def test_create_requires_budget(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with direct_vm.expect_revert("Budget must be funded"):
        c.create_job(_addr(direct_bob), "Ship it", "Working dashboard")


def test_create_requires_title_and_criteria(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = BUDGET
    with direct_vm.expect_revert("Title is required"):
        c.create_job(_addr(direct_bob), "", "Working dashboard")
    with direct_vm.expect_revert("Completion criteria are required"):
        c.create_job(_addr(direct_bob), "Ship it", "")
    direct_vm.value = 0


def test_client_and_coordinator_must_differ(direct_vm, direct_deploy, direct_alice):
    c = _deploy(direct_vm, direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = BUDGET
    with direct_vm.expect_revert("Client and coordinator must differ"):
        c.create_job(_addr(direct_alice), "Ship it", "Working dashboard")
    direct_vm.value = 0


def test_budget_is_custody(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob, budget=2500)
    j = c.get_job(jid)
    assert int(j["budget"]) == 2500
    assert int(j["state"]) == 0                 # OPEN
    assert int(j["total_bp"]) == 0
    assert j["settled"] is False
    assert int(c.get_job_count()) == 1


# ------------------------------------------------- 2. proposing a split ----

def test_only_coordinator_proposes_split(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob)
    direct_vm.sender = direct_alice             # client, not coordinator
    with direct_vm.expect_revert("Only the coordinator can propose the split"):
        c.add_allocation(jid, _addr(direct_alice), 5000, "https://example.com/a")


def test_allocation_validation(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Share must be positive"):
        c.add_allocation(jid, _addr(direct_bob), 0, "https://example.com/a")
    with direct_vm.expect_revert("A valid evidence URL is required"):
        c.add_allocation(jid, _addr(direct_bob), 5000, "not-a-url")
    with direct_vm.expect_revert("Shares would exceed 100%"):
        c.add_allocation(jid, _addr(direct_bob), BP + 1, "https://example.com/a")


def test_allocations_are_booked(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob)
    _alloc(direct_vm, c, direct_bob, jid, direct_alice, 6000, "https://example.com/a")
    _alloc(direct_vm, c, direct_bob, jid, direct_bob, 4000, "https://example.com/b")

    a0 = c.get_allocation(jid, 0)
    a1 = c.get_allocation(jid, 1)
    assert int(a0["share_bp"]) == 6000
    assert int(a1["share_bp"]) == 4000
    assert a0["paid"] is False
    j = c.get_job(jid)
    assert int(j["alloc_count"]) == 2
    assert int(j["total_bp"]) == BP


# ------------------------------------------------- 3. on-chain fairness review ----

def test_review_requires_allocations(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("No allocations proposed"):
        c.review_split(jid)


def test_review_requires_full_hundred_percent(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob)
    _alloc(direct_vm, c, direct_bob, jid, direct_alice, 6000)   # only 60%
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Shares must sum to exactly 100%"):
        c.review_split(jid)


def test_review_is_one_shot(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob)
    _alloc(direct_vm, c, direct_bob, jid, direct_alice, 6000, "https://example.com/a")
    _alloc(direct_vm, c, direct_bob, jid, direct_bob, 4000, "https://example.com/b")
    direct_vm.mock_web(r".*example\.com.*", {"status": 200, "body": "<html>delivered dashboard and docs</html>"})
    direct_vm.mock_llm(r".*", {"fair": True, "score": 88,
                               "reason": "Both shares backed by real evidence.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.review_split(jid)
    with direct_vm.expect_revert("Split has already been reviewed"):
        c.review_split(jid)


def test_score_is_clamped(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob)
    _alloc(direct_vm, c, direct_bob, jid, direct_alice, BP)
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>evidence</html>"})
    direct_vm.mock_llm(r".*", {"fair": True, "score": 9999, "reason": "Fine.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.review_split(jid)
    assert int(c.get_job(jid)["score"]) == 100


# ------------------------------------------------- 4. payout ----

def test_fair_split_pays_every_agent(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob, budget=BUDGET)
    _alloc(direct_vm, c, direct_bob, jid, direct_alice, 6000, "https://example.com/a")
    _alloc(direct_vm, c, direct_bob, jid, direct_bob, 4000, "https://example.com/b")
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>dashboard delivered</html>"})
    direct_vm.mock_llm(r".*", {"fair": True, "score": 90,
                               "reason": "Proportionate.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.review_split(jid)
    c.settle_split(jid)

    j = c.get_job(jid)
    assert int(j["state"]) == 3                 # PAID
    assert j["settled"] is True
    assert c.get_allocation(jid, 0)["paid"] is True
    assert c.get_allocation(jid, 1)["paid"] is True


def test_unfair_split_cannot_be_paid(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob)
    _alloc(direct_vm, c, direct_bob, jid, direct_alice, 9500, "https://example.com/thin")
    _alloc(direct_vm, c, direct_bob, jid, direct_bob, 500, "https://example.com/real")
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>almost nothing</html>"})
    direct_vm.mock_llm(r".*", {"fair": False, "score": 15,
                               "reason": "Large share with thin evidence.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.review_split(jid)
    assert int(c.get_job(jid)["state"]) == 2    # REVIEWED_BAD
    with direct_vm.expect_revert("Split is not approved as fair"):
        c.settle_split(jid)


def test_settle_twice_reverts(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob)
    _alloc(direct_vm, c, direct_bob, jid, direct_alice, BP)
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>evidence</html>"})
    direct_vm.mock_llm(r".*", {"fair": True, "score": 80, "reason": "Ok.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.review_split(jid)
    c.settle_split(jid)
    with direct_vm.expect_revert("Job already settled"):
        c.settle_split(jid)


def test_settle_requires_review(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob)
    _alloc(direct_vm, c, direct_bob, jid, direct_alice, BP)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Split is not approved as fair"):
        c.settle_split(jid)


# ------------------------------------------------- 5. refund ----

def test_refund_returns_budget_to_client(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob)
    _alloc(direct_vm, c, direct_bob, jid, direct_alice, 9500, "https://example.com/thin")
    _alloc(direct_vm, c, direct_bob, jid, direct_bob, 500, "https://example.com/real")
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>thin</html>"})
    direct_vm.mock_llm(r".*", {"fair": False, "score": 10,
                               "reason": "Not proportionate.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.review_split(jid)

    # only the client may pull the refund
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the client can claim the refund"):
        c.refund_job(jid)

    direct_vm.sender = direct_alice
    c.refund_job(jid)
    j = c.get_job(jid)
    assert int(j["state"]) == 4                 # REFUNDED
    assert j["settled"] is True


def test_refund_blocked_once_approved(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob)
    _alloc(direct_vm, c, direct_bob, jid, direct_alice, BP)
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>evidence</html>"})
    direct_vm.mock_llm(r".*", {"fair": True, "score": 85, "reason": "Fair.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.review_split(jid)
    with direct_vm.expect_revert("Split was approved; settle it instead"):
        c.refund_job(jid)


def test_refund_after_settlement_reverts(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob)
    _alloc(direct_vm, c, direct_bob, jid, direct_alice, BP)
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>evidence</html>"})
    direct_vm.mock_llm(r".*", {"fair": True, "score": 85, "reason": "Fair.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.review_split(jid)
    c.settle_split(jid)
    with direct_vm.expect_revert("Job already settled"):
        c.refund_job(jid)


# ------------------------------------------------- 6. lifecycle ----

def test_full_split_lifecycle(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Fund -> propose -> review -> pay. Every agent's share is marked paid."""
    c = _deploy(direct_vm, direct_deploy)
    jid = _job(direct_vm, c, direct_alice, direct_bob, budget=10000)
    _alloc(direct_vm, c, direct_bob, jid, direct_alice, 5000, "https://example.com/a")
    _alloc(direct_vm, c, direct_bob, jid, direct_bob, 3000, "https://example.com/b")
    _alloc(direct_vm, c, direct_bob, jid, direct_alice, 2000, "https://example.com/c")
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>concrete deliverables</html>"})
    direct_vm.mock_llm(r".*", {"fair": True, "score": 92,
                               "reason": "All three shares evidenced.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.review_split(jid)
    assert int(c.get_job(jid)["total_bp"]) == BP
    c.settle_split(jid)

    j = c.get_job(jid)
    assert int(j["state"]) == 3
    for i in range(3):
        assert c.get_allocation(jid, i)["paid"] is True


def test_budget_always_leaves_the_contract(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Either the split is paid out or the client is refunded — never stranded."""
    c = _deploy(direct_vm, direct_deploy)

    fair = _job(direct_vm, c, direct_alice, direct_bob, title="Fair job")
    _alloc(direct_vm, c, direct_bob, fair, direct_alice, BP, "https://example.com/fair")
    bad = _job(direct_vm, c, direct_alice, direct_bob, title="Bad job")
    _alloc(direct_vm, c, direct_bob, bad, direct_alice, BP, "https://example.com/bad")

    direct_vm.mock_web(r".*example\.com/fair.*", {"status": 200, "body": "<html>real work</html>"})
    direct_vm.mock_web(r".*example\.com/bad.*", {"status": 200, "body": "<html>nothing</html>"})
    direct_vm.mock_llm(r".*real work.*", {"fair": True, "score": 88, "reason": "Real.", "confidence": "high"})
    direct_vm.mock_llm(r".*nothing.*", {"fair": False, "score": 6, "reason": "No evidence.", "confidence": "high"})

    direct_vm.sender = direct_alice
    c.review_split(fair)
    c.settle_split(fair)
    assert int(c.get_job(fair)["state"]) == 3
    assert c.get_job(fair)["settled"] is True

    c.review_split(bad)
    assert int(c.get_job(bad)["state"]) == 2
    c.refund_job(bad)
    assert int(c.get_job(bad)["state"]) == 4
    assert c.get_job(bad)["settled"] is True
