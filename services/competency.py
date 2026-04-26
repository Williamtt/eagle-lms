import json
from datetime import datetime
from models import (db, LearningEvent, TaskSubmissionSnapshot,
                    Questionnaire, QuestionnaireSubmission, QuestionnaireAnswer,
                    QuestionnaireItem, LearningJournal, OralPresentationAssessment,
                    TeacherReview, TaskSubmission, SelfStudyProposal)


def determine_resubmit_trigger(task_submission_id: int, user_id: int) -> str:
    """
    檢查最近一次 ai_feedback_received 之後是否有 ai_feedback_viewed event。
    回傳值：'submitted_after_feedback' | 'resubmitted_without_feedback_view'
    """
    latest_received = (LearningEvent.query
                       .filter_by(
                           user_id=user_id,
                           event_type='ai_feedback_received',
                           entity_type='task_submission',
                           entity_id=task_submission_id,
                       )
                       .order_by(LearningEvent.created_at.desc())
                       .first())

    if not latest_received:
        return 'resubmitted_without_feedback_view'

    viewed = (LearningEvent.query
              .filter_by(
                  user_id=user_id,
                  event_type='ai_feedback_viewed',
                  entity_type='task_submission',
                  entity_id=task_submission_id,
              )
              .filter(LearningEvent.created_at > latest_received.created_at)
              .first())

    return 'submitted_after_feedback' if viewed else 'resubmitted_without_feedback_view'


def _snapshot_submission(sub, trigger: str, ai_feedback_id: int = None):
    """
    將 TaskSubmission 當下狀態寫入 TaskSubmissionSnapshot。
    payload_json 包含所有文字回答 + 當下 AI 分數。
    必須在 db.session.flush() 之後呼叫，確保 sub.id 已存在。
    """
    # 計算本次是第幾個 revision
    revision_number = TaskSubmissionSnapshot.query.filter_by(
        task_submission_id=sub.id
    ).count() + 1

    # 收集所有文字內容
    payload = {
        'trigger': trigger,
        'revision_number': revision_number,
        'submitted_at': sub.submitted_at.isoformat() if sub.submitted_at else None,
        'question_responses': {
            r.question_id: r.answer
            for r in sub.question_responses
        },
        'reflection_responses': {
            r.question_id: r.answer
            for r in sub.reflection_responses
        },
        'checklist_responses': {
            r.item_id: r.checked
            for r in sub.checklist_responses
        },
        'ai_scores_at_snapshot': None,
    }

    # 附上當下 AI 分數
    if ai_feedback_id:
        from models import AIFeedback
        ai_fb = db.session.get(AIFeedback, ai_feedback_id)
        if ai_fb:
            try:
                payload['ai_scores_at_snapshot'] = json.loads(ai_fb.scores)
            except (json.JSONDecodeError, TypeError):
                payload['ai_scores_at_snapshot'] = None
    else:
        latest_ai = sub.ai_feedbacks.order_by(
            db.text('created_at DESC')
        ).first()
        if latest_ai:
            try:
                payload['ai_scores_at_snapshot'] = json.loads(latest_ai.scores)
            except (json.JSONDecodeError, TypeError):
                payload['ai_scores_at_snapshot'] = None

    snapshot = TaskSubmissionSnapshot(
        task_submission_id  = sub.id,
        revision_number     = revision_number,
        trigger             = trigger,
        payload_json        = json.dumps(payload, ensure_ascii=False),
        last_ai_feedback_id = ai_feedback_id,
    )
    db.session.add(snapshot)


def compute_arcsa_ac_perception(user_id: int) -> dict:
    """
    計算 ARCSA 前後測各構面平均分。

    回傳：
    {
        'pre':  {'attention': 3.2, 'relevance': 4.0, ...} | None,
        'post': {'attention': 3.6, ...} | None,
        'has_pre': bool,
        'has_post': bool,
    }
    QuestionnaireAnswer.value 是 String，需 cast int。
    """
    result = {'pre': None, 'post': None, 'has_pre': False, 'has_post': False}

    for timing, code in [('pre', 'arcsa_pre'), ('post', 'arcsa_post')]:
        q = Questionnaire.query.filter_by(code=code).first()
        if not q:
            continue
        sub = (QuestionnaireSubmission.query
               .filter_by(user_id=user_id, questionnaire_id=q.id)
               .order_by(QuestionnaireSubmission.submitted_at.desc())
               .first())
        if not sub:
            continue

        rows = (db.session.query(QuestionnaireItem.dimension, QuestionnaireAnswer.value)
                .join(QuestionnaireAnswer,
                      QuestionnaireAnswer.item_code == QuestionnaireItem.item_code)
                .filter(
                    QuestionnaireAnswer.submission_id == sub.id,
                    QuestionnaireItem.questionnaire_id == q.id,
                    QuestionnaireItem.scale_type == 'likert5',
                )
                .all())

        dim_vals: dict[str, list[int]] = {}
        for dimension, value in rows:
            try:
                dim_vals.setdefault(dimension, []).append(int(value))
            except (ValueError, TypeError):
                pass

        if dim_vals:
            result[timing] = {dim: sum(vals) / len(vals) for dim, vals in dim_vals.items()}
            result[f'has_{timing}'] = True

    return result


def aggregate_competency(user_id: int, semester: str) -> dict:
    """
    聚合 DP5 能力資料，供能力雷達圖使用。

    - performance：OralPresentationAssessment 四分項平均（finalized 才納入）
    - perception：LearningJournal 第 5 篇 evaluation_json["DP5"]["self_rating"]

    回傳：
    {
        'axes': ['DP5'],
        'performance': {'DP5': 3.75} | {},
        'perception':  {'DP5': 4}    | {},
        'has_performance': bool,
        'has_perception':  bool,
    }
    兩 series 不平均，保持獨立供前端分開繪製。
    """
    result = {
        'axes': [],
        'performance': {},
        'perception':  {},
        'has_performance': False,
        'has_perception':  False,
    }

    # 任務 Rubric performance：實驗組 → TeacherReview.rubric_json；
    # 對照組 → SelfStudyProposal.rubric_json。皆需 finalized。
    axis_vals: dict[str, list[int]] = {}

    def _accumulate(rubric_json):
        if not rubric_json:
            return
        try:
            scores = json.loads(rubric_json)
        except (json.JSONDecodeError, TypeError):
            return
        for axis, score in scores.items():
            if score is None:
                continue
            try:
                axis_vals.setdefault(axis, []).append(int(score))
            except (ValueError, TypeError):
                pass

    reviews = (TeacherReview.query
               .join(TaskSubmission, TeacherReview.task_submission_id == TaskSubmission.id)
               .filter(
                   TaskSubmission.user_id == user_id,
                   TaskSubmission.semester == semester,
                   TeacherReview.rubric_finalized_at != None,
               )
               .all())
    for review in reviews:
        _accumulate(review.rubric_json)

    proposals = (SelfStudyProposal.query
                 .filter(
                     SelfStudyProposal.user_id == user_id,
                     SelfStudyProposal.semester == semester,
                     SelfStudyProposal.finalized_at != None,
                 )
                 .all())
    for proposal in proposals:
        _accumulate(proposal.rubric_json)

    for axis, vals in axis_vals.items():
        result['performance'][axis] = sum(vals) / len(vals)
        result['has_performance'] = True

    # DP5 performance：口頭報告教師評分（必須 finalized）
    oral = (OralPresentationAssessment.query
            .filter_by(user_id=user_id, semester=semester)
            .first())
    if oral and oral.finalized_at:
        scores = [s for s in [oral.score_content, oral.score_structure,
                               oral.score_delivery, oral.score_qa]
                  if s is not None]
        if scores:
            result['performance']['DP5'] = sum(scores) / len(scores)
            result['has_performance'] = True

    # DP5 perception：日誌第 5 篇自評
    journal5 = (LearningJournal.query
                .filter_by(user_id=user_id, journal_number=5, semester=semester)
                .order_by(LearningJournal.submitted_at.desc())
                .first())
    if journal5 and journal5.evaluation_json:
        try:
            ev = json.loads(journal5.evaluation_json)
            rating = ev.get('DP5', {}).get('self_rating')
            if rating is not None:
                result['perception']['DP5'] = int(rating)
                result['has_perception'] = True
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    # 合併所有出現的 axes，DP5 固定排最後
    all_axes = sorted(
        set(result['performance']) | set(result['perception']) | {'DP5'},
        key=lambda a: (a == 'DP5', a)
    )
    result['axes'] = all_axes

    return result
