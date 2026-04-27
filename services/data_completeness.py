"""
研究資料完整性檢查 (v2.7.0 §3.5 / §3.3 共用)

提供單一進入點 student_completeness(user, semester)，回傳該學生缺漏項目清單。
/teacher/data-check 與 /teacher/export/research-bundle 共用此邏輯。
"""

import json
from models import (db, User, TaskSubmission, TeacherReview, LearningJournal,
                    OralPresentationAssessment, SelfStudyProposal,
                    Questionnaire, QuestionnaireSubmission, LearningEvent)


# 必填項目 ID 命名（前端、CSV、報告共用）
EXP_REQUIRED = [
    'arcsa_pre', 'arcsa_post',
    'task1_submitted', 'task2_submitted', 'task3_submitted', 'task4_submitted',
    'task1_rubric_finalized', 'task2_rubric_finalized',
    'task3_rubric_finalized', 'task4_rubric_finalized',
    'journal1', 'journal2', 'journal3', 'journal4', 'journal5',
    'journal5_dp5_self_rating',
    'oral_finalized',
    'satisfaction',
]

CTRL_REQUIRED = [
    'arcsa_pre', 'arcsa_post',
    'proposal1_finalized', 'proposal2_finalized',
    'proposal3_finalized', 'proposal4_finalized',
    'journal1', 'journal2', 'journal3', 'journal4', 'journal5',
    'journal5_dp5_self_rating',
    'oral_finalized',
    'satisfaction',
]

# 顯示用中文標籤
LABELS = {
    'arcsa_pre':                'ARCSA 前測',
    'arcsa_post':               'ARCSA 後測',
    'task1_submitted':          '任務 1 提交',
    'task2_submitted':          '任務 2 提交',
    'task3_submitted':          '任務 3 提交',
    'task4_submitted':          '任務 4 提交',
    'task1_rubric_finalized':   '任務 1 教師認證',
    'task2_rubric_finalized':   '任務 2 教師認證',
    'task3_rubric_finalized':   '任務 3 教師認證',
    'task4_rubric_finalized':   '任務 4 教師認證',
    'proposal1_finalized':      '自學提案 1 評閱',
    'proposal2_finalized':      '自學提案 2 評閱',
    'proposal3_finalized':      '自學提案 3 評閱',
    'proposal4_finalized':      '自學提案 4 評閱',
    'journal1':                 '日誌 1',
    'journal2':                 '日誌 2',
    'journal3':                 '日誌 3',
    'journal4':                 '日誌 4',
    'journal5':                 '日誌 5',
    'journal5_dp5_self_rating': '日誌 5 DP5 自評',
    'oral_finalized':           '口頭報告認證',
    'satisfaction':             '滿意度問卷',
    'beacon_anomaly':           'AI 回饋未檢視即重交（異常標記）',
}


def _has_questionnaire(user_id: int, code: str) -> bool:
    q = Questionnaire.query.filter_by(code=code).first()
    if not q:
        return False
    return QuestionnaireSubmission.query.filter_by(
        user_id=user_id, questionnaire_id=q.id
    ).first() is not None


def _journal5_has_dp5(user_id: int, semester: str) -> bool:
    j5 = (LearningJournal.query
          .filter_by(user_id=user_id, journal_number=5, semester=semester)
          .order_by(LearningJournal.submitted_at.desc())
          .first())
    if not j5 or not j5.evaluation_json:
        return False
    try:
        ev = json.loads(j5.evaluation_json)
        return ev.get('DP5', {}).get('self_rating') is not None
    except (json.JSONDecodeError, TypeError):
        return False


def _has_journal(user_id: int, n: int, semester: str) -> bool:
    return LearningJournal.query.filter_by(
        user_id=user_id, journal_number=n, semester=semester
    ).first() is not None


def _has_oral_finalized(user_id: int, semester: str) -> bool:
    oral = OralPresentationAssessment.query.filter_by(
        user_id=user_id, semester=semester
    ).first()
    return bool(oral and oral.finalized_at)


def _exp_task_status(user_id: int, task_number: int, semester: str):
    """回傳 (submitted, rubric_finalized, submission_id_or_none)。"""
    sub = (TaskSubmission.query
           .filter(
               TaskSubmission.user_id == user_id,
               TaskSubmission.task_number == task_number,
               TaskSubmission.semester == semester,
               TaskSubmission.status != 'draft',
           )
           .order_by(TaskSubmission.submitted_at.desc())
           .first())
    if not sub:
        return False, False, None
    review = (TeacherReview.query
              .filter_by(task_submission_id=sub.id)
              .filter(TeacherReview.rubric_finalized_at != None)
              .first())
    return True, review is not None, sub.id


def _proposal_finalized(user_id: int, n: int, semester: str) -> bool:
    p = SelfStudyProposal.query.filter_by(
        user_id=user_id, proposal_number=n, semester=semester
    ).first()
    return bool(p and p.finalized_at)


def _beacon_anomaly(user_id: int) -> bool:
    """
    異常：ai_feedback_received 之後 7 天內無 ai_feedback_viewed，但有 task_resubmitted。
    任一 submission 出現此情形即標記。
    """
    received_events = LearningEvent.query.filter_by(
        user_id=user_id,
        event_type='ai_feedback_received',
        entity_type='task_submission',
    ).all()
    for ev in received_events:
        viewed = (LearningEvent.query
                  .filter_by(
                      user_id=user_id,
                      event_type='ai_feedback_viewed',
                      entity_type='task_submission',
                      entity_id=ev.entity_id,
                  )
                  .filter(LearningEvent.created_at > ev.created_at)
                  .first())
        if viewed:
            continue
        resubmit = (LearningEvent.query
                    .filter_by(
                        user_id=user_id,
                        event_type='task_resubmitted',
                        entity_type='task_submission',
                        entity_id=ev.entity_id,
                    )
                    .filter(LearningEvent.created_at > ev.created_at)
                    .first())
        if resubmit:
            return True
    return False


def student_completeness(user: User, semester: str) -> dict:
    """
    回傳該生資料完整性摘要。

    {
        'user_id': int,
        'student_id': str,
        'name': str,
        'class_group': str,
        'experimental_group': 'experimental' | 'control' | None,
        'required': [...],            # 該分組必填項目 id list
        'completed': [...],            # 已完成項目 id list
        'missing':   [...],            # 缺漏項目 id list
        'links':     {missing_id: url_path or ''},  # 缺漏項目補件連結
        'anomalies': [...],            # 額外異常標記（如 beacon_anomaly）
        'research_eligible': bool,    # missing == [] 且 anomalies == []
    }
    """
    group = user.experimental_group
    if group == 'experimental':
        required = list(EXP_REQUIRED)
    elif group == 'control':
        required = list(CTRL_REQUIRED)
    else:
        required = []

    completed = []
    links = {}

    if 'arcsa_pre' in required and _has_questionnaire(user.id, 'arcsa_pre'):
        completed.append('arcsa_pre')
    if 'arcsa_post' in required and _has_questionnaire(user.id, 'arcsa_post'):
        completed.append('arcsa_post')
    if 'satisfaction' in required and _has_questionnaire(user.id, 'satisfaction'):
        completed.append('satisfaction')

    if group == 'experimental':
        for n in (1, 2, 3, 4):
            submitted, finalized, sub_id = _exp_task_status(user.id, n, semester)
            if submitted:
                completed.append(f'task{n}_submitted')
            if finalized:
                completed.append(f'task{n}_rubric_finalized')
            if not finalized and sub_id:
                links[f'task{n}_rubric_finalized'] = f'/teacher/review/{sub_id}'

    if group == 'control':
        for n in (1, 2, 3, 4):
            if _proposal_finalized(user.id, n, semester):
                completed.append(f'proposal{n}_finalized')
            else:
                p = SelfStudyProposal.query.filter_by(
                    user_id=user.id, proposal_number=n, semester=semester
                ).first()
                if p:
                    links[f'proposal{n}_finalized'] = f'/teacher/self-study/{p.id}'

    for n in (1, 2, 3, 4, 5):
        if _has_journal(user.id, n, semester):
            completed.append(f'journal{n}')

    if 'journal5_dp5_self_rating' in required and _journal5_has_dp5(user.id, semester):
        completed.append('journal5_dp5_self_rating')

    if 'oral_finalized' in required:
        if _has_oral_finalized(user.id, semester):
            completed.append('oral_finalized')

    missing = [r for r in required if r not in completed]

    anomalies = []
    if group == 'experimental' and _beacon_anomaly(user.id):
        anomalies.append('beacon_anomaly')

    return {
        'user_id':            user.id,
        'student_id':         user.student_id,
        'name':               user.name,
        'class_group':        user.class_group,
        'experimental_group': group,
        'required':           required,
        'completed':          completed,
        'missing':            missing,
        'links':              links,
        'anomalies':          anomalies,
        'research_eligible':  bool(group) and not missing and not anomalies,
    }


def all_students_completeness(semester: str) -> list:
    """回傳所有 active 學生（含 experimental + control）的完整性摘要清單。"""
    students = (User.query
                .filter_by(role='student', status='active')
                .order_by(User.experimental_group, User.class_group, User.student_id)
                .all())
    return [student_completeness(s, semester) for s in students]
