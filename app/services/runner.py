"""三道工序：取回 → 修整 → 入账。

- 取回（fetch）：只把站点原文行放进中转筐 ``staging_items``，不碰入账册
  ``crawl_records``。
- 修整（clean）：抹掉题名两头的空白；修整后不足 4 字的行留在中转筐并记下
  缘故；与前面已放行题名重复的行也留在中转筐（沿用基线去重语义）。
- 入账（commit）：只把 ``status='released'`` 的行写进入账册。

工序能否推进、能否重复点击，全部由本模块裁决；路由与页面按钮只是提示。
"""

import json

from flask import current_app

from app.db import db_transaction, get_db
from app.services.dedup import normalize_item_key
from app.services.mock_catalog import CATALOGS, parse_catalog_html, render_catalog_page

STAGES = ("fetch", "clean", "commit")
STAGE_LABELS = {"fetch": "取回", "clean": "修整", "commit": "入账"}
MIN_TITLE_CHARS = 4

STAGE_STATE_LABELS = {
    "pending": "待执行",
    "running": "进行中",
    "done": "已完成",
    "failed": "未做成",
}
JOB_STATUS_LABELS = {
    "draft": "草稿",
    "running": "执行中",
    "failed": "有工序未做成",
    "fetched": "取回已完成",
    "cleaned": "修整已完成",
    "done": "已完成",
}


# ---------------------------------------------------------------------------
# 工序行与计数
# ---------------------------------------------------------------------------


def ensure_stages(job_id: int) -> bool:
    """为任务补三行工序进度；任务不存在时返回 False。"""
    db = get_db()
    job = db.execute("SELECT id FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        return False
    for stage in STAGES:
        db.execute(
            "INSERT OR IGNORE INTO job_stages (job_id, stage, state) "
            "VALUES (?, ?, 'pending')",
            (job_id, stage),
        )
    db.commit()
    return True


def _stage_row(db, job_id: int, stage: str):
    return db.execute(
        "SELECT * FROM job_stages WHERE job_id = ? AND stage = ?",
        (job_id, stage),
    ).fetchone()


def _counts(db, job_id: int) -> dict:
    basket = db.execute(
        """
        SELECT
          COUNT(*) AS total,
          COALESCE(SUM(CASE WHEN status = 'released' THEN 1 ELSE 0 END), 0) AS released,
          COALESCE(SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END), 0) AS blocked,
          COALESCE(SUM(CASE WHEN reject_kind = 'duplicate_title' THEN 1 ELSE 0 END), 0) AS duplicates
        FROM staging_items WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    ledger = db.execute(
        "SELECT COUNT(*) AS c FROM crawl_records WHERE job_id = ?", (job_id,)
    ).fetchone()["c"]
    return {
        "basket_total": basket["total"],
        "released": basket["released"],
        "blocked": basket["blocked"],
        "duplicates": basket["duplicates"],
        "ledger": ledger,
    }


def _finish_stage(
    job_id: int,
    stage: str,
    job_status: str,
    rows_total: int,
    rows_ok: int,
    rows_blocked: int,
    job_done: bool = False,
) -> None:
    with db_transaction() as tx:
        tx.execute(
            """
            UPDATE job_stages
            SET state = 'done', reason = NULL,
                rows_total = ?, rows_ok = ?, rows_blocked = ?,
                finished_at = datetime('now')
            WHERE job_id = ? AND stage = ?
            """,
            (rows_total, rows_ok, rows_blocked, job_id, stage),
        )
        if job_done:
            tx.execute(
                "UPDATE crawl_jobs SET status = 'done', finished_at = datetime('now') "
                "WHERE id = ?",
                (job_id,),
            )
        else:
            tx.execute(
                "UPDATE crawl_jobs SET status = ? WHERE id = ?",
                (job_status, job_id),
            )


def _fail_stage(job_id: int, stage: str, reason: str) -> None:
    db = get_db()
    counts = _counts(db, job_id)
    if stage == "fetch":
        total, ok, blocked = counts["basket_total"], 0, 0
    elif stage == "clean":
        total = counts["basket_total"]
        ok, blocked = counts["released"], counts["blocked"]
    else:
        total, ok, blocked = counts["released"], counts["ledger"], 0
    with db_transaction() as tx:
        tx.execute(
            """
            UPDATE job_stages
            SET state = 'failed', reason = ?,
                rows_total = ?, rows_ok = ?, rows_blocked = ?,
                finished_at = datetime('now')
            WHERE job_id = ? AND stage = ?
            """,
            (reason, total, ok, blocked, job_id, stage),
        )
        tx.execute(
            "UPDATE crawl_jobs SET status = 'failed', finished_at = NULL WHERE id = ?",
            (job_id,),
        )


# ---------------------------------------------------------------------------
# 后台裁决
# ---------------------------------------------------------------------------


def _gate(db, job_id: int, stage: str) -> None:
    """只在这里决定能否进入某道工序；不满足就抛 ValueError。"""
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise ValueError("任务不存在")
    label = STAGE_LABELS[stage]
    if job["status"] == "done":
        raise ValueError("任务已完成（三道工序都通过），不能再执行工序")

    row = _stage_row(db, job_id, stage)
    index = STAGES.index(stage)
    if index > 0:
        prev = STAGES[index - 1]
        prev_row = _stage_row(db, job_id, prev)
        if prev_row["state"] != "done":
            raise ValueError(
                f"上一道工序「{STAGE_LABELS[prev]}」尚未完成，不能进入「{label}」"
            )
    if row["state"] == "done":
        raise ValueError(f"工序「{label}」已完成，不能重复执行")
    if row["state"] == "running":
        raise ValueError(f"工序「{label}」正在执行，请稍后再试")


def _claim(db, job_id: int, stage: str) -> bool:
    """原子占位：只有 pending/failed 的工序可以被本次请求接手。"""
    cur = db.execute(
        """
        UPDATE job_stages
        SET state = 'running', reason = NULL,
            started_at = datetime('now'), finished_at = NULL
        WHERE job_id = ? AND stage = ? AND state IN ('pending', 'failed')
        """,
        (job_id, stage),
    )
    db.commit()
    return cur.rowcount == 1


def run_stage(job_id: int, stage: str) -> dict:
    """执行一道工序；越级、重复、正在执行都会被后台拒绝。"""
    if stage not in STAGES:
        raise ValueError(f"未知工序：{stage}")
    if not ensure_stages(job_id):
        raise ValueError("任务不存在")

    db = get_db()
    _gate(db, job_id, stage)
    if not _claim(db, job_id, stage):
        raise ValueError(f"工序「{STAGE_LABELS[stage]}」正在执行，请稍后再试")

    db.execute(
        "UPDATE crawl_jobs SET status = 'running', finished_at = NULL WHERE id = ?",
        (job_id,),
    )
    db.commit()

    label = STAGE_LABELS[stage]
    try:
        if stage == "fetch":
            result = _run_fetch(job_id)
        elif stage == "clean":
            result = _run_clean(job_id)
        else:
            result = _run_commit(job_id)
    except Exception as exc:  # 记下缘故，让页面回来还能看到
        reason = str(exc) or exc.__class__.__name__
        _fail_stage(job_id, stage, reason)
        current_app.logger.warning(
            "job %s stage %s failed: %s", job_id, stage, reason
        )
        raise ValueError(f"「{label}」未做成：{reason}") from exc

    current_app.logger.info("job %s stage %s done", job_id, stage)
    return result


# ---------------------------------------------------------------------------
# 各道工序的具体动作
# ---------------------------------------------------------------------------


def _run_fetch(job_id: int) -> dict:
    db = get_db()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    seed = job["seed_path"]
    if seed not in CATALOGS:
        raise ValueError(f"种子未在站内目录注册：{seed}")

    page = render_catalog_page(seed)
    if isinstance(page, tuple):
        page, status = page
        if status != 200:
            raise ValueError(f"取回站点返回 HTTP {status}")
    items = parse_catalog_html(page, seed)

    inserted = 0
    with db_transaction() as tx:
        for item in items:
            # 以 sku 为自然键：中断后重做取回时，中转筐里已有的原文不会再来一份。
            cur = tx.execute(
                """
                INSERT OR IGNORE INTO staging_items
                    (job_id, sku, raw_title, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    job_id,
                    item["sku"],
                    item["title"],
                    json.dumps(item, ensure_ascii=False),
                ),
            )
            inserted += cur.rowcount

    counts = _counts(db, job_id)
    _finish_stage(
        job_id, "fetch", "fetched",
        rows_total=counts["basket_total"],
        rows_ok=inserted,
        rows_blocked=0,
    )
    return {
        "stage": "fetch",
        "state": "done",
        "basket_total": counts["basket_total"],
        "new_rows": inserted,
        "ledger": counts["ledger"],
        "message": (
            f"取回完成：中转筐共 {counts['basket_total']} 行原文"
            f"（本次新增 {inserted} 行）；入账册仍为 {counts['ledger']} 条"
        ),
    }


def _run_clean(job_id: int) -> dict:
    db = get_db()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    seed = job["seed_path"]
    rows = db.execute(
        "SELECT * FROM staging_items WHERE job_id = ? ORDER BY id", (job_id,)
    ).fetchall()

    released_by_key: dict[str, str] = {}
    total = ok = blocked = 0
    with db_transaction() as tx:
        for row in rows:
            total += 1
            raw_title = row["raw_title"] or ""
            trimmed = raw_title.strip()
            key = normalize_item_key(seed, trimmed)
            reject_kind = None
            reason = None
            status = "released"

            if len(trimmed) < MIN_TITLE_CHARS:
                status = "rejected"
                reject_kind = "short_title"
                reason = (
                    f"题名抹去两端空白后只有 {len(trimmed)} 个字"
                    f"（不足 {MIN_TITLE_CHARS} 字），停在中转筐"
                )
            elif key in released_by_key:
                status = "rejected"
                reject_kind = "duplicate_title"
                reason = (
                    f"题名与已放行的 {released_by_key[key]} 重复，"
                    "停在中转筐"
                )
            else:
                released_by_key[key] = row["sku"]
                ok += 1

            if status == "rejected":
                blocked += 1

            tx.execute(
                """
                UPDATE staging_items
                SET title = ?, status = ?, reject_kind = ?, reason = ?,
                    cleaned_at = datetime('now')
                WHERE id = ?
                """,
                (trimmed, status, reject_kind, reason, row["id"]),
            )

    counts = _counts(db, job_id)
    _finish_stage(
        job_id, "clean", "cleaned",
        rows_total=total,
        rows_ok=ok,
        rows_blocked=blocked,
    )
    return {
        "stage": "clean",
        "state": "done",
        "rows_total": total,
        "released": ok,
        "blocked": blocked,
        "message": (
            f"修整完成：处理 {total} 行，放行 {ok} 行，"
            f"停在筐中 {blocked} 行；入账册仍为 {counts['ledger']} 条"
        ),
    }


def _run_commit(job_id: int) -> dict:
    db = get_db()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    seed = job["seed_path"]
    rows = db.execute(
        """
        SELECT * FROM staging_items
        WHERE job_id = ? AND status = 'released'
        ORDER BY id
        """,
        (job_id,),
    ).fetchall()

    inserted = 0
    with db_transaction() as tx:
        for row in rows:
            title = row["title"]
            key = normalize_item_key(seed, title)
            payload = {
                "sku": row["sku"],
                "title": title,
                "raw_title": row["raw_title"],
                "seed_path": seed,
            }
            # 中断后重做入账时，已写过的行不会重复写。
            cur = tx.execute(
                """
                INSERT OR IGNORE INTO crawl_records
                    (job_id, item_key, title, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, key, title, json.dumps(payload, ensure_ascii=False)),
            )
            inserted += cur.rowcount

    counts = _counts(db, job_id)
    _finish_stage(
        job_id, "commit", "done",
        rows_total=len(rows),
        rows_ok=counts["ledger"],
        rows_blocked=0,
        job_done=True,
    )
    return {
        "stage": "commit",
        "state": "done",
        "released": len(rows),
        "new_rows": inserted,
        "ledger": counts["ledger"],
        "message": (
            f"入账完成：本次新增 {inserted} 条，入账册共 {counts['ledger']} 条"
            "；三道工序都通过，任务结束"
        ),
    }


# ---------------------------------------------------------------------------
# 页面进度与兼容入口
# ---------------------------------------------------------------------------


def stage_overview(job_id: int) -> tuple[list[dict], dict, dict | None]:
    """给页面用的工序视图；能否点按钮也在这里算好。"""
    ensure_stages(job_id)
    db = get_db()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        return [], {}, None

    rows = {
        stage: _stage_row(db, job_id, stage)
        for stage in STAGES
    }
    counts = _counts(db, job_id)
    first_open = next(
        (stage for stage in STAGES if rows[stage]["state"] != "done"), None
    )
    first_label = STAGE_LABELS[first_open] if first_open else None

    stages: list[dict] = []
    for index, stage in enumerate(STAGES):
        row = rows[stage]
        label = STAGE_LABELS[stage]
        state = row["state"]
        can_run = False
        resume = False
        hint = ""

        if job["status"] == "done":
            hint = "任务已完成，三道工序都通过"
        elif state == "done":
            hint = "已完成"
        elif stage != first_open:
            hint = f"「{first_label}」尚未完成，本道按钮不可用（后台同样拒绝）"
        elif state == "running":
            hint = "正在执行，请稍后再试"
        elif state == "failed":
            can_run = True
            resume = True
            hint = "这道没做成，可从中断处继续；续做不会重新取回中转筐里的原文"
        elif state == "pending":
            can_run = True
            hint = "上一道已完成，可以执行"
        else:
            hint = "已完成"

        if stage == "fetch":
            detail = (
                f"中转筐 {counts['basket_total']} 行原文 · 入账册 {counts['ledger']} 条"
            )
        elif stage == "clean":
            detail = (
                f"处理 {row['rows_total']} 行 · 放行 {row['rows_ok']} 行 · "
                f"停在筐中 {row['rows_blocked']} 行"
            )
        else:
            detail = (
                f"放行 {row['rows_total']} 行 · 入账册 {counts['ledger']} 条"
            )

        if resume:
            button_label = f"从「{label}」中断处继续"
        else:
            button_label = f"执行「{label}」"

        stages.append(
            {
                "key": stage,
                "label": label,
                "state": state,
                "state_label": STAGE_STATE_LABELS[state],
                "can_run": can_run,
                "resume": resume,
                "hint": hint,
                "reason": row["reason"],
                "detail": detail,
                "rows_total": row["rows_total"],
                "rows_ok": row["rows_ok"],
                "rows_blocked": row["rows_blocked"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "button_label": button_label,
            }
        )

    return stages, counts, job


def run_job(job_id: int) -> dict:
    """兼容入口：按顺序跑完剩余各道；某道失败就停在该道。"""
    if not ensure_stages(job_id):
        raise ValueError("job not found")

    db = get_db()
    for stage in STAGES:
        row = _stage_row(db, job_id, stage)
        if row["state"] == "done":
            continue
        try:
            run_stage(job_id, stage)
        except Exception as exc:
            counts = _counts(db, job_id)
            return {
                "status": "failed",
                "reason": str(exc),
                "stage": stage,
                "inserted": counts["ledger"],
                "skipped_dup": counts["duplicates"],
                "total_source": counts["basket_total"],
                "released": counts["released"],
            }

    counts = _counts(db, job_id)
    return {
        "status": "done",
        "inserted": counts["ledger"],
        "skipped_dup": counts["duplicates"],
        "total_source": counts["basket_total"],
        "released": counts["released"],
    }
