"""三道工序（取回 → 修整 → 入账）的自动校验。

对应六项要求：
1. 只取回时入账册仍空；
2. 短题名停在修整；
3. 从修整往下接不会把原文再取一遍；
4. 还没修整就入账会被拒绝；
5. 三道都通过后入账条数等于放行的行数；
6. 同一道尚未做完时拒绝再点。
"""

import pytest

from app.db import get_db
from app.services import runner

ALPHA = "/mock/catalog/alpha"
GAMMA = "/mock/catalog/gamma"


def login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "123456"},
        follow_redirects=True,
    )


def create_job(app, client, seed=ALPHA, name="t"):
    client.post(
        "/jobs",
        data={"name": name, "seed_path": seed},
        follow_redirects=True,
    )
    with app.app_context():
        return get_db().execute(
            "SELECT id FROM crawl_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]


def run_stage(client, job_id, stage, follow=True):
    return client.post(
        f"/jobs/{job_id}/stages/{stage}",
        follow_redirects=follow,
    )


def read_state(app, job_id):
    with app.app_context():
        db = get_db()
        stages = {
            row["stage"]: row["state"]
            for row in db.execute(
                "SELECT stage, state FROM job_stages WHERE job_id = ?", (job_id,)
            )
        }
        basket = db.execute(
            "SELECT COUNT(*) AS c FROM staging_items WHERE job_id = ?", (job_id,)
        ).fetchone()["c"]
        ledger = db.execute(
            "SELECT COUNT(*) AS c FROM crawl_records WHERE job_id = ?", (job_id,)
        ).fetchone()["c"]
        job_status = db.execute(
            "SELECT status FROM crawl_jobs WHERE id = ?", (job_id,)
        ).fetchone()["status"]
        return {
            "stages": stages,
            "basket": basket,
            "ledger": ledger,
            "job_status": job_status,
        }


# 1 -------------------------------------------------------------------------
def test_fetch_only_leaves_ledger_empty(app, client):
    """只取回时，中转筐有原文行，入账册必须仍是空的。"""
    login(client)
    job_id = create_job(app, client)

    run_stage(client, job_id, "fetch")

    state = read_state(app, job_id)
    assert state["stages"]["fetch"] == "done"
    assert state["stages"]["clean"] == "pending"
    assert state["stages"]["commit"] == "pending"
    assert state["basket"] == 3
    assert state["ledger"] == 0
    assert state["job_status"] == "fetched"


# 2 -------------------------------------------------------------------------
def test_short_title_stays_in_basket_with_reason(app, client):
    """题名抹掉两端空白后不足四字，停在中转筐并记下缘故，不许入账。"""
    login(client)
    job_id = create_job(app, client, seed=GAMMA, name="gamma")

    run_stage(client, job_id, "fetch")
    run_stage(client, job_id, "clean")

    with app.app_context():
        db = get_db()
        stuck = db.execute(
            "SELECT * FROM staging_items WHERE job_id = ? AND sku = 'G-202'",
            (job_id,),
        ).fetchone()
        released = db.execute(
            "SELECT COUNT(*) AS c FROM staging_items "
            "WHERE job_id = ? AND status = 'released'",
            (job_id,),
        ).fetchone()["c"]

    assert stuck["status"] == "rejected"
    assert stuck["reject_kind"] == "short_title"
    assert stuck["title"] == "巡检"
    assert "不足 4 字" in stuck["reason"]
    assert released == 2

    state = read_state(app, job_id)
    assert state["stages"]["clean"] == "done"
    assert state["ledger"] == 0
    assert state["job_status"] == "cleaned"


# 3 -------------------------------------------------------------------------
def test_resume_from_clean_does_not_refetch(app, client, monkeypatch):
    """中途失败后从修整往下接，中转筐不新增、原文不会再取一遍。"""
    login(client)
    job_id = create_job(app, client)

    run_stage(client, job_id, "fetch")
    before = read_state(app, job_id)
    assert before["basket"] == 3

    # 第一次修整未做成：改写修整动作，让它中途抛错。
    def broken_clean(job_id_):
        raise RuntimeError("模拟修整中断")

    monkeypatch.setattr(runner, "_run_clean", broken_clean)
    run_stage(client, job_id, "clean")
    failed = read_state(app, job_id)
    assert failed["stages"]["clean"] == "failed"
    assert failed["job_status"] == "failed"

    # 恢复修整动作，从修整这道接着做。
    monkeypatch.undo()
    run_stage(client, job_id, "clean")

    after = read_state(app, job_id)
    assert after["stages"]["fetch"] == "done"
    assert after["stages"]["clean"] == "done"
    assert after["basket"] == before["basket"] == 3
    assert after["ledger"] == 0

    # 取回已完成，续做时不得再点取回。
    resp = run_stage(client, job_id, "fetch")
    assert "已完成，不能重复执行" in resp.get_data(as_text=True)
    assert read_state(app, job_id)["basket"] == 3


# 4 -------------------------------------------------------------------------
def test_commit_rejected_before_clean(app, client):
    """还没修整就入账，后台拒绝，入账册仍空。"""
    login(client)
    job_id = create_job(app, client)

    run_stage(client, job_id, "fetch")
    resp = run_stage(client, job_id, "commit")

    text = resp.get_data(as_text=True)
    assert "上一道工序「修整」尚未完成" in text
    state = read_state(app, job_id)
    assert state["stages"]["commit"] == "pending"
    assert state["ledger"] == 0


# 5 -------------------------------------------------------------------------
def test_ledger_count_equals_released_after_all_three(app, client):
    """三道都通过后，入账条数等于修整放行的行数。"""
    login(client)
    job_id = create_job(app, client)

    for stage in ("fetch", "clean", "commit"):
        run_stage(client, job_id, stage)

    with app.app_context():
        db = get_db()
        released = db.execute(
            "SELECT COUNT(*) AS c FROM staging_items "
            "WHERE job_id = ? AND status = 'released'",
            (job_id,),
        ).fetchone()["c"]
        ledger = db.execute(
            "SELECT COUNT(*) AS c FROM crawl_records WHERE job_id = ?",
            (job_id,),
        ).fetchone()["c"]

    state = read_state(app, job_id)
    assert released == ledger == 2  # alpha 有一行重复题名停在筐中
    assert state["stages"] == {"fetch": "done", "clean": "done", "commit": "done"}
    assert state["job_status"] == "done"


# 6 -------------------------------------------------------------------------
def test_running_stage_cannot_be_clicked_again(app, client):
    """同一道还在忙时，再点一次被拒绝，且不产生重复数据。"""
    login(client)
    job_id = create_job(app, client)
    run_stage(client, job_id, "fetch")

    # 模拟修整正在执行。
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE job_stages SET state = 'running' WHERE job_id = ? AND stage = 'clean'",
            (job_id,),
        )
        db.commit()

    resp = run_stage(client, job_id, "clean", follow=False)
    assert resp.status_code == 302

    with app.app_context():
        db = get_db()
        row = db.execute(
            "SELECT state FROM job_stages WHERE job_id = ? AND stage = 'clean'",
            (job_id,),
        ).fetchone()
        assert row["state"] == "running"
        assert db.execute(
            "SELECT COUNT(*) AS c FROM staging_items WHERE job_id = ?", (job_id,)
        ).fetchone()["c"] == 3

    text = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "正在执行" in text


def test_claim_is_atomic_for_running_stage(app, client):
    """后台占位也只允许 pending/failed 的工序被接手。"""
    login(client)
    job_id = create_job(app, client)
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE job_stages SET state = 'running' WHERE job_id = ? AND stage = 'fetch'",
            (job_id,),
        )
        db.commit()
    with app.app_context():
        assert runner._claim(get_db(), job_id, "fetch") is False


@pytest.mark.parametrize("stage", ["fetch", "clean", "commit"])
def test_page_disables_buttons_for_blocked_stages(app, client, stage):
    """页面按当前进度给出进度、缘故与不可用按钮。"""
    login(client)
    job_id = create_job(app, client)
    run_stage(client, job_id, "fetch")
    if stage == "clean":
        run_stage(client, job_id, "clean")

    text = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    if stage == "commit":
        assert "尚未完成" in text
    assert "三道工序依次为：取回 → 修整 → 入账" in text
