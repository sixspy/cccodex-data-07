from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.db import get_db
from app.routes.auth import login_required
from app.services.mock_catalog import CATALOGS
from app.services.runner import (
    JOB_STATUS_LABELS,
    STAGE_LABELS,
    STAGES,
    run_job,
    run_stage,
    stage_overview,
)

bp = Blueprint("jobs", __name__)


@bp.route("/")
@login_required
def list_jobs():
    db = get_db()
    jobs = db.execute(
        "SELECT * FROM crawl_jobs ORDER BY id DESC"
    ).fetchall()
    counts = {
        row["job_id"]: row["c"]
        for row in db.execute(
            "SELECT job_id, COUNT(*) AS c FROM crawl_records GROUP BY job_id"
        )
    }
    return render_template(
        "jobs_list.html",
        jobs=jobs,
        counts=counts,
        seeds=sorted(CATALOGS.keys()),
        job_status_labels=JOB_STATUS_LABELS,
    )


@bp.post("/jobs")
@login_required
def create_job():
    name = request.form.get("name", "").strip()
    seed = request.form.get("seed_path", "").strip()
    if not name or not seed:
        flash("请填写任务名与种子路径", "error")
        return redirect(url_for("jobs.list_jobs"))
    if seed not in CATALOGS:
        flash("种子路径必须是已注册的 mock 目录", "error")
        return redirect(url_for("jobs.list_jobs"))
    db = get_db()
    db.execute(
        "INSERT INTO crawl_jobs (name, seed_path, status) VALUES (?, ?, 'draft')",
        (name, seed),
    )
    db.commit()
    flash("任务已创建", "ok")
    return redirect(url_for("jobs.list_jobs"))


@bp.post("/jobs/<int:job_id>/stages/<stage>")
@login_required
def run_stage_view(job_id: int, stage: str):
    """单道工序入口；三道工序的裁决全部在后台。"""
    if stage not in STAGES:
        flash(f"未知工序：{stage}", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))

    label = STAGE_LABELS[stage]
    try:
        summary = run_stage(job_id, stage)
        flash(summary["message"], "ok")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:  # 兜底：页面只显示一句，进度与缘故已落库
        flash(f"「{label}」未做成：{e}", "error")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/jobs/<int:job_id>/run")
@login_required
def run_job_view(job_id: int):
    """兼容入口：从第一道未完成的工序往下接，直到某道未做成或全部通过。"""
    try:
        summary = run_job(job_id)
        if summary["status"] == "done":
            flash(
                f"三道工序完成：入账 {summary['inserted']} 条，"
                f"去重跳过 {summary['skipped_dup']} 条",
                "ok",
            )
        else:
            flash(summary["reason"], "error")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"运行失败: {e}", "error")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.get("/jobs/<int:job_id>")
@login_required
def detail(job_id: int):
    db = get_db()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        flash("任务不存在", "error")
        return redirect(url_for("jobs.list_jobs"))

    stages, counts, job = stage_overview(job_id)
    records = db.execute(
        "SELECT * FROM crawl_records WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    basket = db.execute(
        "SELECT * FROM staging_items WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    return render_template(
        "job_detail.html",
        job=job,
        stages=stages,
        counts=counts,
        records=records,
        basket=basket,
        job_status_labels=JOB_STATUS_LABELS,
        stage_state_labels={
            "pending": "待执行",
            "running": "进行中",
            "done": "已完成",
            "failed": "未做成",
        },
    )
