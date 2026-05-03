"""AI 預批服務（v2.8.0）

責任：
- ensure_ai_draft：學生 submit 完成後或教師打開評閱頁時呼叫；幂等
- schedule_background_draft：spawn daemon thread 跑 ensure_ai_draft；best-effort
- batch_pregenerate_drafts / batch_worker：教師按下「預先生成」批次處理

設計重點：
- L1 background thread 是 best-effort；失敗由 L2/L3 補位
- 兩個 AI 呼叫整合為一次 ensure_ai_draft（review suggestion + rubric）
- 每次 AI call 都會寫 AIUsageLog（成功與失敗都寫）
- Quota 80% soft ceiling 觸發後拒絕新預生（教師 fallback 不受影響）
"""

from __future__ import annotations
import json
import logging
import threading
from datetime import datetime
from typing import Optional

from flask import current_app

import ai_service
from models import (db, TaskSubmission, AIReviewSuggestion,
                    AIBatchJob, TeacherReview)
from services import ai_quota

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 內部小工具
# ─────────────────────────────────────────────────────────────────────────────

def _build_text(sub: TaskSubmission, task_def: dict) -> str:
    """將結構化提交內容組為 AI 評閱用文字。lazy import 避免循環依賴。"""
    from app import _build_submission_text_for_ai
    return _build_submission_text_for_ai(sub, task_def)


def _is_cache_fresh(cache: AIReviewSuggestion, sub: TaskSubmission) -> bool:
    """cache 是否仍可用：suggestion 與 rubric 都有 + source_updated_at 不舊。"""
    if not cache:
        return False
    if not cache.suggestion or not cache.ai_rubric_scores_json:
        return False
    if cache.source_updated_at is None or cache.source_updated_at < sub.updated_at:
        return False
    return True


def _is_finalized_review(sub: TaskSubmission) -> bool:
    """教師已 finalize → 不再覆蓋 cache（保留原始 anchoring 證據）。"""
    review = sub.teacher_reviews.filter(
        TeacherReview.rubric_finalized_at.isnot(None)
    ).first()
    return review is not None


# ─────────────────────────────────────────────────────────────────────────────
# 核心：單筆預生
# ─────────────────────────────────────────────────────────────────────────────

def ensure_ai_draft(sub: TaskSubmission,
                    *,
                    force: bool = False,
                    triggered_by_user_id: Optional[int] = None) -> Optional[AIReviewSuggestion]:
    """確保 sub 有 AI 草稿（suggestion + rubric）。

    流程：
    1. 已 finalized → 直接回傳既有 cache（不重生）
    2. cache 命中且 force=False → 直接回
    3. quota 拒絕 → 寫失敗 log → 回 None
    4. 呼叫兩個 AI（review suggestion + rubric）→ 寫快取 → 寫成功 log

    回傳 AIReviewSuggestion 或 None（quota 拒絕、AI 全失敗、提交內容為空）。
    """
    from task_definitions import TASKS, AXES_DESCRIPTIONS

    cache = AIReviewSuggestion.query.filter_by(task_submission_id=sub.id).first()

    # 已 finalized：保留 cache 原貌（即使 source_updated_at 比 sub.updated_at 舊）
    if _is_finalized_review(sub):
        return cache

    if cache and not force and _is_cache_fresh(cache, sub):
        return cache

    # quota check
    allowed, reason = ai_quota.can_call()
    if not allowed:
        ai_quota.record_call(
            purpose='review_suggestion',
            model_used='',
            input_tokens=0,
            output_tokens=0,
            task_submission_id=sub.id,
            user_id=triggered_by_user_id,
            success=False,
            error_message=f'quota_blocked: {reason[:200]}',
            commit=True,
        )
        logger.warning('[ai_grading] quota blocked sub=%s: %s', sub.id, reason)
        return None

    task_def = TASKS.get(sub.task_number, {})
    if not task_def:
        return None

    text = _build_text(sub, task_def)
    if not text.strip():
        return None

    axes = task_def.get('axes', []) or []

    # ─── (1) review suggestion ──────────────────────────────────────────────
    # 用 try/except 把例外都收進來，避免 raise 時跳出讓已 add 的 log 漏寫
    sug_result = {}
    sug_usage = {}
    sug_error = None
    sug_text = ''
    try:
        sug_result = ai_service.generate_review_suggestion(
            text, sub.task_number, 'structured'
        )
        if not isinstance(sug_result, dict):
            sug_result = {'suggestion': str(sug_result)}
        sug_usage = sug_result.get('_usage', {}) or {}
        sug_error = sug_result.get('_error')
        sug_text = sug_result.get('suggestion') or ''
    except Exception as e:  # noqa: BLE001
        sug_error = str(e)
        logger.exception('[ai_grading] review_suggestion raised sub=%s', sub.id)
    is_sug_error = bool(sug_error) or sug_text.startswith('AI 建議生成失敗') or not sug_text

    ai_quota.record_call(
        purpose='review_suggestion',
        model_used=sug_usage.get('model', ''),
        input_tokens=sug_usage.get('input_tokens', 0),
        output_tokens=sug_usage.get('output_tokens', 0),
        task_submission_id=sub.id,
        user_id=triggered_by_user_id,
        success=not is_sug_error,
        error_message=str(sug_error or '')[:500] if is_sug_error else '',
        commit=False,
    )

    # ─── (2) rubric ─────────────────────────────────────────────────────────
    rub_result = {'rubric_scores': {}, 'comment': ''}
    rub_usage = {}
    rub_error = None
    if axes:
        try:
            rub_result = ai_service.generate_self_study_rubric_suggestion(
                text, axes, AXES_DESCRIPTIONS
            )
            if not isinstance(rub_result, dict):
                rub_result = {'rubric_scores': {}, 'comment': str(rub_result)}
            rub_usage = rub_result.get('_usage', {}) or {}
            rub_error = rub_result.get('error') or rub_result.get('_error')
        except Exception as e:  # noqa: BLE001
            rub_error = str(e)
            logger.exception('[ai_grading] rubric_suggestion raised sub=%s', sub.id)
        ai_quota.record_call(
            purpose='rubric_suggestion',
            model_used=rub_usage.get('model', ''),
            input_tokens=rub_usage.get('input_tokens', 0),
            output_tokens=rub_usage.get('output_tokens', 0),
            task_submission_id=sub.id,
            user_id=triggered_by_user_id,
            success=not rub_error,
            error_message=str(rub_error or '')[:500] if rub_error else '',
            commit=False,
        )

    # ─── (3) 寫入 cache ────────────────────────────────────────────────────
    # 規則：
    #   - 兩個都失敗 → 不寫 cache（但 usage log 仍 commit）
    #   - 任一成功 → 只寫成功的部分；source_updated_at 只在「兩個都成功（或 axes
    #     為空且 sug 成功）」才前進；否則保留舊值，下次仍會重算缺的部分。
    if is_sug_error and (not axes or rub_error):
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return None

    rubric_scores = rub_result.get('rubric_scores') or {}
    rubric_comment = rub_result.get('comment') or ''
    has_valid_rubric = bool(not rub_error and rubric_scores)

    # 「全部成功」= sug 成功 AND（無 axes 或 rubric 成功）
    all_succeeded = (not is_sug_error) and (not axes or has_valid_rubric)

    if cache:
        if not is_sug_error:
            cache.raw_json        = json.dumps(sug_result, ensure_ascii=False, default=str)
            cache.suggestion      = sug_text
            cache.suggested_score = sug_result.get('suggested_score')
            cache.rubric_notes    = sug_result.get('rubric_notes') or ''
            cache.model_used      = sug_usage.get('model', 'claude-sonnet-4-5')
        if has_valid_rubric:
            cache.ai_rubric_scores_json = json.dumps(rubric_scores, ensure_ascii=False)
            cache.ai_rubric_comment     = rubric_comment
        # 只有「兩個都成功」才前進 source_updated_at；否則維持舊值，
        # 下次仍會被 _is_cache_fresh() 視為失效而重算缺的部分。
        if all_succeeded:
            cache.source_updated_at = sub.updated_at
        cache.created_at = datetime.utcnow()
    else:
        # 新建 cache：source_updated_at 只在 all_succeeded 時設成新值；
        # 否則設成 epoch（明確表達「未完成」），下次必然重算
        from datetime import datetime as _dt
        epoch = _dt(1970, 1, 1)
        cache = AIReviewSuggestion(
            task_submission_id    = sub.id,
            raw_json              = json.dumps(sug_result, ensure_ascii=False, default=str)
                                    if not is_sug_error else '{}',
            suggestion            = sug_text if not is_sug_error else '',
            suggested_score       = sug_result.get('suggested_score') if not is_sug_error else None,
            rubric_notes          = sug_result.get('rubric_notes') or '' if not is_sug_error else '',
            source_updated_at     = sub.updated_at if all_succeeded else epoch,
            model_used            = sug_usage.get('model', 'claude-sonnet-4-5'),
            ai_rubric_scores_json = json.dumps(rubric_scores, ensure_ascii=False) if has_valid_rubric else '',
            ai_rubric_comment     = rubric_comment if has_valid_rubric else '',
        )
        db.session.add(cache)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception('[ai_grading] commit failed sub=%s', sub.id)
        return None
    return cache


# ─────────────────────────────────────────────────────────────────────────────
# L1：學生 submit 後背景 thread
# ─────────────────────────────────────────────────────────────────────────────

def schedule_background_draft(sub_id: int) -> None:
    """spawn daemon thread → ensure_ai_draft。

    Best-effort：失敗無聲；學生 submit 已完成不受影響。
    若 gunicorn worker recycle，由 L2（dashboard 預生）/L3（lazy fallback）補位。
    """
    app = current_app._get_current_object()

    def runner():
        try:
            with app.app_context():
                sub = db.session.get(TaskSubmission, sub_id)
                if sub and sub.status == 'submitted':
                    ensure_ai_draft(sub)
        except Exception as e:  # noqa: BLE001
            logger.exception('[ai_grading] background draft failed sub=%s: %s', sub_id, e)
        finally:
            try:
                db.session.remove()
            except Exception:
                pass

    t = threading.Thread(target=runner, daemon=True,
                         name=f'ai-draft-bg-{sub_id}')
    t.start()


# ─────────────────────────────────────────────────────────────────────────────
# L2：教師批次預生
# ─────────────────────────────────────────────────────────────────────────────

def list_pending_submissions() -> list[TaskSubmission]:
    """列出『本學期已 submitted、未 finalized rubric、cache 缺或失效』的提交。"""
    from task_definitions import SEMESTER as _SEM
    subs = TaskSubmission.query.filter_by(status='submitted', semester=_SEM).all()
    pending = []
    for s in subs:
        if _is_finalized_review(s):
            continue
        cache = AIReviewSuggestion.query.filter_by(task_submission_id=s.id).first()
        if not _is_cache_fresh(cache, s):
            pending.append(s)
    return pending


def batch_pregenerate_drafts(teacher_id: int) -> int:
    """建立 AIBatchJob，spawn daemon thread 跑 batch_worker；回傳 job_id。

    教師可在 /teacher/batch/status/<job_id> 輪詢進度。
    """
    pending = list_pending_submissions()
    job = AIBatchJob(
        teacher_id = teacher_id,
        status     = 'pending',
        total      = len(pending),
    )
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    if not pending:
        job.status = 'done'
        job.finished_at = datetime.utcnow()
        db.session.commit()
        return job_id

    sub_ids = [s.id for s in pending]
    app = current_app._get_current_object()

    def worker():
        try:
            with app.app_context():
                _run_batch(job_id, sub_ids, teacher_id)
        except Exception as e:  # noqa: BLE001
            logger.exception('[ai_grading] batch worker fatal job=%s: %s', job_id, e)
            try:
                with app.app_context():
                    j = db.session.get(AIBatchJob, job_id)
                    if j:
                        j.status = 'failed'
                        j.last_error = str(e)[:500]
                        j.finished_at = datetime.utcnow()
                        db.session.commit()
            except Exception:
                pass
        finally:
            try:
                db.session.remove()
            except Exception:
                pass

    t = threading.Thread(target=worker, daemon=True,
                         name=f'ai-batch-worker-{job_id}')
    t.start()
    return job_id


def _run_batch(job_id: int, sub_ids: list[int], teacher_id: int) -> None:
    """thread 內主迴圈：每筆呼叫 ensure_ai_draft，失敗 retry 一次。"""
    job = db.session.get(AIBatchJob, job_id)
    if not job:
        return
    job.status = 'running'
    db.session.commit()

    processed = skipped = failed = 0
    last_err = ''

    for sid in sub_ids:
        sub = db.session.get(TaskSubmission, sid)
        if not sub:
            skipped += 1
            continue

        # quota 提前 short-circuit（避免無謂 retry）
        allowed, reason = ai_quota.can_call()
        if not allowed:
            failed += 1
            last_err = f'quota: {reason[:120]}'
            break  # 後續全部停止；前端可看到 last_error

        try:
            cache = ensure_ai_draft(sub, triggered_by_user_id=teacher_id)
            if cache and cache.suggestion:
                processed += 1
            else:
                # retry 一次
                cache = ensure_ai_draft(sub, force=True, triggered_by_user_id=teacher_id)
                if cache and cache.suggestion:
                    processed += 1
                else:
                    failed += 1
                    last_err = 'ai_returned_empty'
        except Exception as e:  # noqa: BLE001
            failed += 1
            last_err = str(e)[:200]
            logger.exception('[ai_grading] batch sub=%s failed: %s', sid, e)

        # 每 10 筆 commit 一次更新 progress
        if (processed + skipped + failed) % 10 == 0:
            job = db.session.get(AIBatchJob, job_id)
            if job:
                job.processed = processed
                job.skipped   = skipped
                job.failed    = failed
                job.last_error = last_err
                db.session.commit()

    # 收尾
    job = db.session.get(AIBatchJob, job_id)
    if job:
        job.processed   = processed
        job.skipped     = skipped
        job.failed      = failed
        job.last_error  = last_err
        job.status      = 'done'
        job.finished_at = datetime.utcnow()
        db.session.commit()
