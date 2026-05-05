#!/usr/bin/env python3
"""v2.9.0 smoke test：AI 預批 + 教師覆核 + 額度管理 + ZIP 匯出 + cache freshness 語意。

執行：
    cd /Users/william/Documents/myCodes/MyEAGLE/eagle-lms
    SECRET_KEY=t TEACHER_CODE=t python3 tests/smoke_test_v280.py

前置：
    - 本地 SQLite 已 migrate 過 v2.9.0 schema（自動 _run_migrations，含 content_updated_at）
    - 不需 ANTHROPIC_API_KEY；測試用 monkeypatch 替換 ai_service
"""
import os
import sys
import json
import io
import zipfile
from datetime import datetime, timedelta
from unittest.mock import patch

# 確保 eagle-lms 在 sys.path
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault('SECRET_KEY', 't')
os.environ.setdefault('TEACHER_CODE', 't')
os.environ.setdefault('ANTHROPIC_API_KEY', 'fake-for-test')

from app import app  # noqa: E402
from models import (db, User, TaskSubmission, AIReviewSuggestion,  # noqa: E402
                    TeacherReview, AIUsageLog, AIQuotaOverride, AIBatchJob)
from services import ai_quota, ai_grading  # noqa: E402

# ─── helper ───────────────────────────────────────────────────────────────
PASS = 0
FAIL = 0
RESULTS = []


def check(label, cond, detail=''):
    global PASS, FAIL
    ok = bool(cond)
    if ok:
        PASS += 1
        print(f'[PASS] {label}')
    else:
        FAIL += 1
        print(f'[FAIL] {label}{(": " + detail) if detail else ""}')
    RESULTS.append((label, ok, detail))


def fake_review(text, task_num, kind):
    return {
        'suggestion': '【FAKE AI】請覆核此段。',
        'suggested_score': 85.0,
        'rubric_notes': '依據各軸評分標準。',
        '_usage': {'input_tokens': 1000, 'output_tokens': 200, 'model': 'fake-claude'},
    }


def fake_rubric(text, axes, axes_desc):
    return {
        'rubric_scores': {ax: 4 for ax in axes},
        'comment': '【FAKE rubric】',
        '_usage': {'input_tokens': 500, 'output_tokens': 100, 'model': 'fake-claude'},
    }


# ─── tests ────────────────────────────────────────────────────────────────

def reset_state(sub_id):
    """清掉特定 sub 的 cache + review 以利重測。"""
    AIReviewSuggestion.query.filter_by(task_submission_id=sub_id).delete()
    er = TeacherReview.query.filter_by(task_submission_id=sub_id).first()
    if er:
        er.feedback = ''
        er.rubric_json = ''
        er.rubric_finalized_at = None
        er.ai_initial_feedback_snapshot = ''
        er.ai_initial_rubric_snapshot = ''
        er.teacher_first_opened_at = None
        er.teacher_modified = False
        er.dwell_seconds = 0
    db.session.commit()


def main():
    with app.app_context():
        # 清空 v2.8.0 audit 表（避免污染）
        AIUsageLog.query.delete()
        AIQuotaOverride.query.delete()
        AIBatchJob.query.delete()
        db.session.commit()

        sub = TaskSubmission.query.first()
        teacher = User.query.filter_by(role='teacher').first()
        assert sub and teacher, '測試需要至少 1 個 sub + 1 個教師'

        # 暫存原始狀態
        original_status = sub.status
        original_er = TeacherReview.query.filter_by(task_submission_id=sub.id).first()
        if original_er:
            backup = {
                'feedback': original_er.feedback,
                'rubric_json': original_er.rubric_json,
                'rubric_finalized_at': original_er.rubric_finalized_at,
            }
        sub.status = 'submitted'
        db.session.commit()
        reset_state(sub.id)

        try:
            # ── 測試 1：ensure_ai_draft 寫入快取 + AIUsageLog ────────────
            with patch('ai_service.generate_review_suggestion', fake_review), \
                 patch('ai_service.generate_self_study_rubric_suggestion', fake_rubric):
                cache = ai_grading.ensure_ai_draft(sub, triggered_by_user_id=teacher.id)
            check('1) ensure_ai_draft 寫入 cache（suggestion + rubric）',
                  cache and cache.suggestion and cache.ai_rubric_scores_json,
                  detail=str(cache))
            check('1b) AIUsageLog 寫入 2 筆（review + rubric）',
                  AIUsageLog.query.count() == 2)

            # ── 測試 2：cache 命中（不再呼叫 AI）─────────────────────────
            calls = {'n': 0}

            def fake_review_count(*a, **kw):
                calls['n'] += 1
                return fake_review(*a, **kw)

            with patch('ai_service.generate_review_suggestion', fake_review_count), \
                 patch('ai_service.generate_self_study_rubric_suggestion', fake_rubric):
                cache2 = ai_grading.ensure_ai_draft(sub, triggered_by_user_id=teacher.id)
            check('2) 第二次呼叫命中 cache（未呼叫 AI）',
                  calls['n'] == 0 and cache2.id == cache.id)

            # ── 測試 3a：sub.updated_at 後移（content_updated_at 不變）→ 不觸發 AI ──
            # v2.9.0：cache freshness 以 content_updated_at 判斷；教師動作只改 updated_at，
            # 不應讓 cache 失效（這是修 bug 前舊行為的 regression test）
            sub.updated_at = datetime.utcnow() + timedelta(seconds=1)
            db.session.commit()
            calls['n'] = 0
            with patch('ai_service.generate_review_suggestion', fake_review_count), \
                 patch('ai_service.generate_self_study_rubric_suggestion', fake_rubric):
                ai_grading.ensure_ai_draft(sub, triggered_by_user_id=teacher.id)
            check('3a) sub.updated_at 後移（content_updated_at 不變）→ 不觸發 AI 重算',
                  calls['n'] == 0)

            # ── 測試 3b：sub.content_updated_at 後移 → cache 失效重算 ────────
            sub.content_updated_at = datetime.utcnow() + timedelta(seconds=2)
            db.session.commit()
            calls['n'] = 0
            with patch('ai_service.generate_review_suggestion', fake_review_count), \
                 patch('ai_service.generate_self_study_rubric_suggestion', fake_rubric):
                ai_grading.ensure_ai_draft(sub, triggered_by_user_id=teacher.id)
            check('3b) sub.content_updated_at 後移（學生重交）→ 觸發 AI 重算',
                  calls['n'] == 1)

            # ── 測試 3c：模擬教師發布評閱（status→reviewed 更新 updated_at）→ 不觸發 AI ─
            # regression：此為 bug 修前舊行為；現在 updated_at 不影響 cache
            sub.status = 'reviewed'
            db.session.commit()   # onupdate 會更新 updated_at；content_updated_at 不變
            db.session.refresh(sub)
            calls['n'] = 0
            with patch('ai_service.generate_review_suggestion', fake_review_count), \
                 patch('ai_service.generate_self_study_rubric_suggestion', fake_rubric):
                ai_grading.ensure_ai_draft(sub, triggered_by_user_id=teacher.id)
            check('3c) 教師發布評閱（updated_at 更新）→ AI cache 仍有效，不重跑',
                  calls['n'] == 0)
            sub.status = 'submitted'
            db.session.commit()

            # ── 測試 4：AI 失敗 fallback（兩個都失敗 → 不寫 cache）───────
            reset_state(sub.id)

            def fail_review(*a, **kw):
                return {'suggestion': 'AI 建議生成失敗：x', '_error': 'x', '_usage': {}}

            def fail_rubric(*a, **kw):
                return {'error': 'x', 'rubric_scores': {}, 'comment': 'x',
                        '_error': 'x', '_usage': {}}

            with patch('ai_service.generate_review_suggestion', fail_review), \
                 patch('ai_service.generate_self_study_rubric_suggestion', fail_rubric):
                cache_after_fail = ai_grading.ensure_ai_draft(sub, triggered_by_user_id=teacher.id)
            check('4) 兩個 AI 都失敗 → 不寫快取，且 usage log 記失敗',
                  cache_after_fail is None
                  and AIUsageLog.query.filter_by(success=False).count() >= 2)

            # ── 測試 5：教師 finalize 後不被 AI 覆寫 ────────────────────
            reset_state(sub.id)
            with patch('ai_service.generate_review_suggestion', fake_review), \
                 patch('ai_service.generate_self_study_rubric_suggestion', fake_rubric):
                ai_grading.ensure_ai_draft(sub, triggered_by_user_id=teacher.id)
            er = TeacherReview.query.filter_by(task_submission_id=sub.id).first()
            if not er:
                er = TeacherReview(task_submission_id=sub.id, teacher_id=teacher.id,
                                   feedback='教師手寫', rubric_json='{"DP1": 5}',
                                   rubric_finalized_at=datetime.utcnow())
                db.session.add(er)
            else:
                er.feedback = '教師手寫'
                er.rubric_json = '{"DP1": 5}'
                er.rubric_finalized_at = datetime.utcnow()
            db.session.commit()

            old_cache = AIReviewSuggestion.query.filter_by(task_submission_id=sub.id).first()
            old_sug = old_cache.suggestion if old_cache else None
            # 模擬學生重交（content_updated_at 後移）；教師已 finalize 仍不應重算
            sub.content_updated_at = datetime.utcnow() + timedelta(seconds=2)
            db.session.commit()
            with patch('ai_service.generate_review_suggestion', fake_review), \
                 patch('ai_service.generate_self_study_rubric_suggestion', fake_rubric):
                ai_grading.ensure_ai_draft(sub, triggered_by_user_id=teacher.id)
            new_cache = AIReviewSuggestion.query.filter_by(task_submission_id=sub.id).first()
            check('5) finalized 後 ensure_ai_draft 不重生（保留原 cache）',
                  new_cache.suggestion == old_sug)

            # ── 測試 6：背景 thread 不阻塞（schedule_background_draft）─
            reset_state(sub.id)
            er = TeacherReview.query.filter_by(task_submission_id=sub.id).first()
            if er:
                er.rubric_finalized_at = None
                db.session.commit()

            import time as _t
            t0 = _t.time()
            ai_grading.schedule_background_draft(sub.id)
            elapsed = _t.time() - t0
            check('6) schedule_background_draft 立即返回（< 0.5 秒）',
                  elapsed < 0.5, detail=f'elapsed={elapsed:.3f}s')

            # ── 測試 7：token cap 80% 拒絕 ─────────────────────────────
            reset_state(sub.id)
            cap = ai_quota.cap_for_period()
            big = AIUsageLog(period=ai_quota.current_period(), purpose='test',
                             model_used='x', input_tokens=int(cap * 0.85),
                             output_tokens=0, success=True)
            db.session.add(big)
            db.session.commit()
            allowed, reason = ai_quota.can_call()
            check('7) 80% soft ceiling 拒絕 can_call',
                  allowed is False and 'soft ceiling' in reason)

            # 驗 ensure_ai_draft 受阻
            with patch('ai_service.generate_review_suggestion', fake_review), \
                 patch('ai_service.generate_self_study_rubric_suggestion', fake_rubric):
                blocked_cache = ai_grading.ensure_ai_draft(sub, triggered_by_user_id=teacher.id)
            check('7b) cap 觸發後 ensure_ai_draft 返回 None', blocked_cache is None)

            # ── 測試 8：cap override 即時生效 ──────────────────────────
            ovr = AIQuotaOverride(period=ai_quota.current_period(),
                                  extra_tokens=10_000_000,
                                  approved_by=teacher.id, reason='test')
            db.session.add(ovr)
            db.session.commit()
            allowed, reason = ai_quota.can_call()
            check('8) cap override 後 can_call 重新 allow', allowed is True)

            # ── 測試 9（bonus）：ZIP 含新增 CSV ────────────────────────
            with app.test_client() as c:
                with c.session_transaction() as s:
                    s['_user_id'] = str(teacher.id)
                    s['_csrf_token'] = 'tt'
                r = c.get('/teacher/export/research-bundle')
            ok = r.status_code == 200
            if ok:
                zf = zipfile.ZipFile(io.BytesIO(r.data))
                names = zf.namelist()
                ok = ('_supplementary/ai_usage_log.csv' in names
                      and '_supplementary/ai_batch_jobs.csv' in names
                      and '_supplementary/ai_review_suggestions.csv' in names)
                if ok:
                    tr_csv = zf.read('teacher_reviews.csv').decode('utf-8')
                    ok = 'teacher_modified' in tr_csv and 'dwell_seconds' in tr_csv
                    # Codex Q6：ai_review_suggestions.csv 應該有 research_eligible 欄位
                    ars_csv = zf.read('_supplementary/ai_review_suggestions.csv').decode('utf-8')
                    ok = ok and 'research_eligible' in ars_csv.split('\n')[0]
            check('9) research-bundle ZIP 含 v2.8.0 三張新 CSV + anchoring + ai_review_suggestions 含 research_eligible', ok)

            # ── 測試 10（Codex Q2）：rubric 失敗、suggestion 成功 → source_updated_at 不前進 ─
            # 重置 quota 環境
            AIUsageLog.query.delete()
            AIQuotaOverride.query.delete()
            db.session.commit()
            reset_state(sub.id)
            er = TeacherReview.query.filter_by(task_submission_id=sub.id).first()
            if er:
                er.rubric_finalized_at = None
                db.session.commit()
            sub.content_updated_at = datetime.utcnow()
            db.session.commit()
            content_updated_at_before = sub.content_updated_at

            def fail_rubric_only(*a, **kw):
                return {'error': 'fake-fail', 'rubric_scores': {}, 'comment': 'x',
                        '_error': 'fake-fail', '_usage': {'input_tokens': 100, 'output_tokens': 0,
                                                          'model': 'fake'}}

            with patch('ai_service.generate_review_suggestion', fake_review), \
                 patch('ai_service.generate_self_study_rubric_suggestion', fail_rubric_only):
                cache_partial = ai_grading.ensure_ai_draft(sub, triggered_by_user_id=teacher.id)
            from task_definitions import TASKS as _TASKS
            has_axes = bool(_TASKS.get(sub.task_number, {}).get('axes', []))
            if has_axes:
                # 有 axes：rubric 失敗 → source_updated_at 不應前進到 content_updated_at_before
                # （新 cache 會被設成 epoch；舊 cache 會保留舊 source_updated_at）
                ok10 = (cache_partial is not None
                        and cache_partial.suggestion
                        and cache_partial.source_updated_at < content_updated_at_before)
                check('10) rubric 失敗 → source_updated_at 不前進（保留 stale 標記以便重算）', ok10)
            else:
                # 沒 axes：sug 成功就算 all_succeeded
                check('10) (skipped, task has no axes)', True)

            # ── 測試 11（Codex Q8b）：list_pending_submissions 限 SEMESTER ─
            from task_definitions import SEMESTER as _SEM
            pending = ai_grading.list_pending_submissions()
            ok11 = all(p.semester == _SEM for p in pending)
            check('11) list_pending_submissions 全部 semester=當前學期',
                  ok11, detail=f'len={len(pending)}')

            # ── 測試 12（Codex Q8a）：舊端點走 ensure_ai_draft 治理 ─────
            # 清空 log 後呼叫 /ai_suggestion，應該寫一筆 review_suggestion log
            AIUsageLog.query.delete()
            AIReviewSuggestion.query.filter_by(task_submission_id=sub.id).delete()
            db.session.commit()
            er2 = TeacherReview.query.filter_by(task_submission_id=sub.id).first()
            if er2:
                er2.rubric_finalized_at = None
                db.session.commit()
            with patch('ai_service.generate_review_suggestion', fake_review), \
                 patch('ai_service.generate_self_study_rubric_suggestion', fake_rubric):
                with app.test_client() as c:
                    with c.session_transaction() as s:
                        s['_user_id'] = str(teacher.id)
                        s['_csrf_token'] = 'tt'
                    r = c.get(f'/teacher/review/{sub.id}/ai_suggestion')
            ok12 = (r.status_code == 200
                    and AIUsageLog.query.filter_by(purpose='review_suggestion').count() >= 1)
            check('12) 舊 /ai_suggestion 端點走 ensure_ai_draft（會寫 usage log）', ok12)

        finally:
            # 清還原狀
            AIUsageLog.query.delete()
            AIQuotaOverride.query.delete()
            AIBatchJob.query.delete()
            AIReviewSuggestion.query.filter_by(task_submission_id=sub.id).delete()
            sub.status = original_status
            er = TeacherReview.query.filter_by(task_submission_id=sub.id).first()
            if original_er and er:
                er.feedback = backup['feedback']
                er.rubric_json = backup['rubric_json']
                er.rubric_finalized_at = backup['rubric_finalized_at']
                er.ai_initial_feedback_snapshot = ''
                er.ai_initial_rubric_snapshot = ''
                er.teacher_first_opened_at = None
                er.teacher_modified = False
                er.dwell_seconds = 0
            elif er and not original_er:
                db.session.delete(er)
            db.session.commit()

    print(f'\n=== smoke_test_v280: {PASS} PASS, {FAIL} FAIL ===')
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == '__main__':
    main()
