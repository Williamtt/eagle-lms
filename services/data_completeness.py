"""
研究資料完整性檢查 (v2.7.0 §3.5 / §3.3 共用)

提供單一進入點 student_completeness(user, semester)，回傳該學生缺漏項目清單。
/teacher/data-check 與 /teacher/export/research-bundle 共用此邏輯。
"""

import json
from datetime import datetime
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


class _Bulk:
    """一次撈齊整批學生所需資料，取代原本 per-student、per-item 的逐項查詢。

    v2.8.1：/teacher/data-check 原本 90 位學生 × 約 20 次查詢 ≈ 1800 次 round-trip。
    改為固定 8 次查詢。單一學生也走同一條路徑（user_ids 只放一個），避免兩套邏輯分歧。
    """

    _MIN_DT = datetime.min

    def __init__(self, user_ids, semester: str):
        self.semester = semester
        ids = list(user_ids)

        # ── 問卷填答 ──
        self.q_by_code = {q.code: q.id for q in Questionnaire.query.all()}
        self.q_done = set()
        if ids:
            self.q_done = {
                (r[0], r[1]) for r in db.session.query(
                    QuestionnaireSubmission.user_id,
                    QuestionnaireSubmission.questionnaire_id,
                ).filter(QuestionnaireSubmission.user_id.in_(ids)).all()
            }

        # ── 任務提交（非草稿）；UniqueConstraint 保證每人每任務至多一筆 ──
        self.task_sub = {}          # (user_id, task_number) -> submission_id
        sub_ids = []
        if ids:
            rows = (TaskSubmission.query
                    .filter(TaskSubmission.user_id.in_(ids),
                            TaskSubmission.semester == semester,
                            TaskSubmission.status != 'draft')
                    .all())
            rows.sort(key=lambda s: s.submitted_at or self._MIN_DT)
            for s in rows:          # 由舊到新掃過，最後寫入者即最新一筆
                self.task_sub[(s.user_id, s.task_number)] = s.id
            sub_ids = [s.id for s in rows]

        self.rubric_finalized = set()
        if sub_ids:
            self.rubric_finalized = {
                r[0] for r in db.session.query(TeacherReview.task_submission_id)
                .filter(TeacherReview.task_submission_id.in_(sub_ids),
                        TeacherReview.rubric_finalized_at.isnot(None)).all()
            }

        # ── 學習日誌（無 unique constraint，取最新一筆）──
        self.journals = {}          # (user_id, journal_number) -> LearningJournal
        if ids:
            js = (LearningJournal.query
                  .filter(LearningJournal.user_id.in_(ids),
                          LearningJournal.semester == semester).all())
            js.sort(key=lambda j: j.submitted_at or self._MIN_DT)
            for j in js:
                self.journals[(j.user_id, j.journal_number)] = j

        # ── 口頭報告（UniqueConstraint user_id + semester）──
        self.oral_finalized = set()
        if ids:
            self.oral_finalized = {
                o.user_id for o in OralPresentationAssessment.query
                .filter(OralPresentationAssessment.user_id.in_(ids),
                        OralPresentationAssessment.semester == semester).all()
                if o.finalized_at
            }

        # ── 自學提案（UniqueConstraint user_id + proposal_number + semester）──
        self.proposals = {}         # (user_id, n) -> (id, finalized_at)
        if ids:
            for p in (SelfStudyProposal.query
                      .filter(SelfStudyProposal.user_id.in_(ids),
                              SelfStudyProposal.semester == semester).all()):
                self.proposals[(p.user_id, p.proposal_number)] = (p.id, p.finalized_at)

        # ── beacon 事件 ──
        self.events = {}            # user_id -> [LearningEvent]
        if ids:
            for e in (LearningEvent.query
                      .filter(LearningEvent.user_id.in_(ids),
                              LearningEvent.entity_type == 'task_submission',
                              LearningEvent.event_type.in_([
                                  'ai_feedback_received',
                                  'ai_feedback_viewed',
                                  'task_resubmitted',
                              ])).all()):
                self.events.setdefault(e.user_id, []).append(e)


def _has_questionnaire(user_id: int, code: str, bulk: _Bulk) -> bool:
    qid = bulk.q_by_code.get(code)
    if qid is None:
        return False
    return (user_id, qid) in bulk.q_done


def _journal5_has_dp5(user_id: int, semester: str, bulk: _Bulk) -> bool:
    j5 = bulk.journals.get((user_id, 5))
    if not j5 or not j5.evaluation_json:
        return False
    try:
        ev = json.loads(j5.evaluation_json)
        return ev.get('DP5', {}).get('self_rating') is not None
    except (json.JSONDecodeError, TypeError):
        return False


def _has_journal(user_id: int, n: int, semester: str, bulk: _Bulk) -> bool:
    return (user_id, n) in bulk.journals


def _has_oral_finalized(user_id: int, semester: str, bulk: _Bulk) -> bool:
    return user_id in bulk.oral_finalized


def _exp_task_status(user_id: int, task_number: int, semester: str, bulk: _Bulk):
    """回傳 (submitted, rubric_finalized, submission_id_or_none)。"""
    sub_id = bulk.task_sub.get((user_id, task_number))
    if not sub_id:
        return False, False, None
    return True, sub_id in bulk.rubric_finalized, sub_id


def _proposal_finalized(user_id: int, n: int, semester: str, bulk: _Bulk) -> bool:
    entry = bulk.proposals.get((user_id, n))
    return bool(entry and entry[1])


def _beacon_anomaly(user_id: int, bulk: _Bulk) -> bool:
    """
    異常：ai_feedback_received 之後無 ai_feedback_viewed，但有 task_resubmitted。
    任一 submission 出現此情形即標記。
    """
    evs = bulk.events.get(user_id, [])
    if not evs:
        return False
    for ev in evs:
        if ev.event_type != 'ai_feedback_received' or ev.created_at is None:
            continue
        later = [e for e in evs
                 if e.entity_id == ev.entity_id
                 and e.created_at is not None
                 and e.created_at > ev.created_at]
        if any(e.event_type == 'ai_feedback_viewed' for e in later):
            continue
        if any(e.event_type == 'task_resubmitted' for e in later):
            return True
    return False


def student_completeness(user: User, semester: str, bulk: '_Bulk' = None) -> dict:
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
    if bulk is None:
        bulk = _Bulk([user.id], semester)

    group = user.experimental_group
    if group == 'experimental':
        required = list(EXP_REQUIRED)
    elif group == 'control':
        required = list(CTRL_REQUIRED)
    else:
        required = []

    completed = []
    links = {}

    if 'arcsa_pre' in required and _has_questionnaire(user.id, 'arcsa_pre', bulk):
        completed.append('arcsa_pre')
    if 'arcsa_post' in required and _has_questionnaire(user.id, 'arcsa_post', bulk):
        completed.append('arcsa_post')
    if 'satisfaction' in required and _has_questionnaire(user.id, 'satisfaction', bulk):
        completed.append('satisfaction')

    if group == 'experimental':
        for n in (1, 2, 3, 4):
            submitted, finalized, sub_id = _exp_task_status(user.id, n, semester, bulk)
            if submitted:
                completed.append(f'task{n}_submitted')
            if finalized:
                completed.append(f'task{n}_rubric_finalized')
            if not finalized and sub_id:
                links[f'task{n}_rubric_finalized'] = f'/teacher/review/{sub_id}'

    if group == 'control':
        for n in (1, 2, 3, 4):
            if _proposal_finalized(user.id, n, semester, bulk):
                completed.append(f'proposal{n}_finalized')
            else:
                entry = bulk.proposals.get((user.id, n))
                if entry:
                    links[f'proposal{n}_finalized'] = f'/teacher/self-study/{entry[0]}'

    for n in (1, 2, 3, 4, 5):
        if _has_journal(user.id, n, semester, bulk):
            completed.append(f'journal{n}')

    if 'journal5_dp5_self_rating' in required and _journal5_has_dp5(user.id, semester, bulk):
        completed.append('journal5_dp5_self_rating')

    if 'oral_finalized' in required:
        if _has_oral_finalized(user.id, semester, bulk):
            completed.append('oral_finalized')

    missing = [r for r in required if r not in completed]

    anomalies = []
    if group == 'experimental' and _beacon_anomaly(user.id, bulk):
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
    bulk = _Bulk([s.id for s in students], semester)
    return [student_completeness(s, semester, bulk) for s in students]
