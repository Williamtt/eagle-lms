from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()


# =============================================================================
# 使用者
# =============================================================================

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id           = db.Column(db.Integer, primary_key=True)
    student_id   = db.Column(db.String(20), unique=True, nullable=False)
    name         = db.Column(db.String(80), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role         = db.Column(db.String(10), default='student')   # student / teacher
    class_group  = db.Column(db.String(20), default='A')         # 營建管理A班 / 營建管理B班
    status       = db.Column(db.String(10), default='active')    # pending / active / disabled
    experimental_group = db.Column(db.String(20), nullable=True) # 'experimental' | 'control' | NULL=未分組
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    consent_agreed_at    = db.Column(db.DateTime, nullable=True)
    reset_requested_at   = db.Column(db.DateTime, nullable=True)
    reset_contact_email  = db.Column(db.String(120), nullable=True)

    # relationships
    task_submissions          = db.relationship('TaskSubmission', backref='author', lazy='dynamic')
    questionnaire_submissions = db.relationship('QuestionnaireSubmission', backref='author', lazy='dynamic')
    learning_journals         = db.relationship('LearningJournal', backref='author', lazy='dynamic')
    reviews                   = db.relationship('TeacherReview', backref='reviewer', lazy='dynamic',
                                                foreign_keys='TeacherReview.teacher_id')

    # 舊版相容
    submissions = db.relationship('Submission', backref='author', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_teacher(self):
        return self.role == 'teacher'


# =============================================================================
# 模組 A：結構化任務提交
# =============================================================================

class TaskSubmission(db.Model):
    """一次完整的任務提交（v2 結構化版本）"""
    __tablename__ = 'task_submissions'
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    task_number  = db.Column(db.Integer, nullable=False)         # 1–4（或未來更多）
    task_version = db.Column(db.String(20), nullable=False)      # 對應 task_definitions 中的版本號，如 "2.0.0"
    semester     = db.Column(db.String(10), nullable=False)      # 如 "114-1"
    status       = db.Column(db.String(20), default='submitted') # draft / submitted / reviewed
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'task_number', 'semester',
                                          name='uq_task_submission_per_semester'),)

    # 子表關聯（cascade 確保刪除 TaskSubmission 時子表一併清除）
    question_responses   = db.relationship('QuestionResponse',   backref='task_submission',
                                           cascade='all, delete-orphan', lazy='dynamic')
    checklist_responses  = db.relationship('ChecklistResponse',  backref='task_submission',
                                           cascade='all, delete-orphan', lazy='dynamic')
    reflection_responses = db.relationship('ReflectionResponse', backref='task_submission',
                                           cascade='all, delete-orphan', lazy='dynamic')
    deliverable_uploads  = db.relationship('DeliverableUpload',  backref='task_submission',
                                           cascade='all, delete-orphan', lazy='dynamic')
    ai_feedbacks         = db.relationship('AIFeedback',  backref='task_submission',
                                           foreign_keys='AIFeedback.task_submission_id', lazy='dynamic')
    teacher_reviews      = db.relationship('TeacherReview', backref='task_submission',
                                           foreign_keys='TeacherReview.task_submission_id', lazy='dynamic')

    @property
    def latest_teacher_review(self):
        return self.teacher_reviews.filter_by(published=True).order_by(
            TeacherReview.reviewed_at.desc()).first()

    @property
    def latest_ai_feedback(self):
        return self.ai_feedbacks.order_by(AIFeedback.created_at.desc()).first()


class QuestionResponse(db.Model):
    """提示問題的逐題回答"""
    __tablename__ = 'question_responses'
    id            = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey('task_submissions.id'), nullable=False)
    question_id   = db.Column(db.String(20), nullable=False)  # 如 "t1_pq1"
    answer        = db.Column(db.Text, default='')


class ChecklistResponse(db.Model):
    """自我檢核清單的逐項勾選"""
    __tablename__ = 'checklist_responses'
    id            = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey('task_submissions.id'), nullable=False)
    item_id       = db.Column(db.String(20), nullable=False)  # 如 "t1_cl1"
    checked       = db.Column(db.Boolean, default=False)      # 保留向後相容；由 status 衍生
    status        = db.Column(db.String(10), default='not_done')  # 'done'/'partial'/'not_done'
    note          = db.Column(db.Text, default='')            # 說明原因（部分/未完成時建議填寫）


class ReflectionResponse(db.Model):
    """當責反思的逐題回答"""
    __tablename__ = 'reflection_responses'
    id            = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey('task_submissions.id'), nullable=False)
    question_id   = db.Column(db.String(20), nullable=False)  # 如 "t1_rq1"
    answer        = db.Column(db.Text, default='')


class DeliverableUpload(db.Model):
    """任務產出的分項繳交"""
    __tablename__ = 'deliverable_uploads'
    id             = db.Column(db.Integer, primary_key=True)
    submission_id  = db.Column(db.Integer, db.ForeignKey('task_submissions.id'), nullable=False)
    deliverable_id = db.Column(db.String(20), nullable=False)  # 如 "t1_d1"
    content        = db.Column(db.Text, default='')            # 文字內容（反思報告）
    file_path      = db.Column(db.String(500), default='')
    file_name      = db.Column(db.String(200), default='')


# =============================================================================
# 模組 B：AI 回饋（統一支援 v1 和 v2）
# =============================================================================

class AIFeedback(db.Model):
    __tablename__ = 'ai_feedbacks'
    id                 = db.Column(db.Integer, primary_key=True)

    # v2：指向 TaskSubmission
    task_submission_id = db.Column(db.Integer, db.ForeignKey('task_submissions.id'), nullable=True)
    # v1：指向舊 Submission（向後相容）
    submission_id      = db.Column(db.Integer, db.ForeignKey('submissions.id'), nullable=True)

    # overall = 整體回饋；per_question = 逐題回饋（question_id 有值）
    feedback_type = db.Column(db.String(20), default='overall')
    feedback      = db.Column(db.Text, nullable=False)
    scores        = db.Column(db.Text, default='')   # JSON：各構面分數
    question_id   = db.Column(db.String(20), default='')  # 逐題回饋時指向哪一題
    model_used    = db.Column(db.String(50), default='claude-sonnet-4-20250514')
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)


# =============================================================================
# 模組 C：教師評閱（統一支援 v1 和 v2）
# =============================================================================

class TeacherReview(db.Model):
    __tablename__ = 'teacher_reviews'
    id                 = db.Column(db.Integer, primary_key=True)

    # v2：指向 TaskSubmission
    task_submission_id = db.Column(db.Integer, db.ForeignKey('task_submissions.id'), nullable=True)
    # v1：指向舊 Submission（向後相容）
    submission_id      = db.Column(db.Integer, db.ForeignKey('submissions.id'), nullable=True)

    teacher_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    feedback      = db.Column(db.Text, default='')
    score         = db.Column(db.Float, nullable=True)  # 0–100
    published     = db.Column(db.Boolean, default=False)
    reviewed_at   = db.Column(db.DateTime, default=datetime.utcnow)
    # v2.5.0：對稱對照組 finalized 機制
    rubric_json         = db.Column(db.Text, default='')        # {"DP1": 4, "EP4": 3, ...}
    rubric_finalized_at = db.Column(db.DateTime, nullable=True) # 教師按「確認」才寫入
    rubric_source       = db.Column(db.String(40), default='')  # 'teacher_manual' | 'ai_adopted_then_confirmed'

    __table_args__ = (
        db.UniqueConstraint('task_submission_id',
                            name='uq_teacher_review_per_submission'),
    )


class AIReviewSuggestion(db.Model):
    """AI 為教師產出的評閱建議（快取，避免每次打開評閱頁都重呼叫 Claude）。
    快取失效條件：source_updated_at 小於 task_submission.updated_at，或教師手動重新產生。
    """
    __tablename__ = 'ai_review_suggestions'
    id                 = db.Column(db.Integer, primary_key=True)
    task_submission_id = db.Column(db.Integer,
                                   db.ForeignKey('task_submissions.id'),
                                   unique=True, nullable=False)
    raw_json           = db.Column(db.Text, nullable=False)   # 完整原始 JSON
    suggestion         = db.Column(db.Text, default='')
    suggested_score    = db.Column(db.Float, nullable=True)
    rubric_notes       = db.Column(db.Text, default='')
    source_updated_at  = db.Column(db.DateTime, nullable=False)  # 產生當下 submission.updated_at
    model_used         = db.Column(db.String(50), default='')
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)


# =============================================================================
# 模組 D：問卷系統
# =============================================================================

class Questionnaire(db.Model):
    """問卷定義（從 questionnaire_definitions.py seed 而來）"""
    __tablename__ = 'questionnaires'
    id         = db.Column(db.Integer, primary_key=True)
    code       = db.Column(db.String(30), unique=True, nullable=False)  # "arcsa_pre"
    name       = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, default='')
    version    = db.Column(db.String(20), nullable=False)
    semester   = db.Column(db.String(10), nullable=False)
    timing     = db.Column(db.String(50), default='')    # 填答時機說明
    is_active  = db.Column(db.Boolean, default=False)    # 教師開放後才能填答
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items       = db.relationship('QuestionnaireItem', backref='questionnaire',
                                  order_by='QuestionnaireItem.order',
                                  cascade='all, delete-orphan')
    submissions = db.relationship('QuestionnaireSubmission', backref='questionnaire', lazy='dynamic')


class QuestionnaireItem(db.Model):
    """問卷中的單一題目"""
    __tablename__ = 'questionnaire_items'
    id               = db.Column(db.Integer, primary_key=True)
    questionnaire_id = db.Column(db.Integer, db.ForeignKey('questionnaires.id'), nullable=False)
    item_code        = db.Column(db.String(30), nullable=False)   # "arcsa_a1"
    dimension        = db.Column(db.String(30), nullable=False)   # "attention"
    dimension_label  = db.Column(db.String(20), default='')       # "引起注意"
    activity         = db.Column(db.String(50), default='')       # 滿意度問卷專用
    text             = db.Column(db.Text, nullable=False)
    scale_type       = db.Column(db.String(20), default='likert5') # likert5 / text / choice
    choices          = db.Column(db.Text, default='')              # JSON array
    order            = db.Column(db.Integer, default=0)
    required         = db.Column(db.Boolean, default=True)


class QuestionnaireSubmission(db.Model):
    """學生的一次問卷填答"""
    __tablename__ = 'questionnaire_submissions'
    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    questionnaire_id = db.Column(db.Integer, db.ForeignKey('questionnaires.id'), nullable=False)
    submitted_at     = db.Column(db.DateTime, default=datetime.utcnow)

    answers = db.relationship('QuestionnaireAnswer', backref='submission',
                              cascade='all, delete-orphan', lazy='dynamic')

    __table_args__ = (db.UniqueConstraint('user_id', 'questionnaire_id',
                                          name='uq_questionnaire_per_user'),)


class QuestionnaireAnswer(db.Model):
    """問卷中單一題目的回答"""
    __tablename__ = 'questionnaire_answers'
    id            = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer,
                              db.ForeignKey('questionnaire_submissions.id'), nullable=False)
    item_code     = db.Column(db.String(30), nullable=False)  # 對應 QuestionnaireItem.item_code
    value         = db.Column(db.String(1000), default='')    # Likert 值（"1"–"5"）或文字回答


# =============================================================================
# 模組 E：學習日誌
# =============================================================================

class LearningJournal(db.Model):
    """學生的學習日誌（全學期共 5 次）"""
    __tablename__ = 'learning_journals'
    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    journal_number = db.Column(db.Integer, nullable=False)     # 1–5
    week           = db.Column(db.Integer, nullable=False)     # 課程週次
    semester       = db.Column(db.String(10), nullable=False)
    content        = db.Column(db.Text, default='')
    submitted_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    evaluation_json = db.Column(db.Text, default='')  # journal 5 DP5 自評 perception（{"DP5": {"self_rating": 4, "evidence": "..."}}）


# =============================================================================
# 舊版相容（v1 Submission 保留，不刪除）
# =============================================================================

class Submission(db.Model):
    """v1 自由文字提交（保留舊資料，不再用於新提交）"""
    __tablename__ = 'submissions'
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    task_number     = db.Column(db.Integer, nullable=False)
    submission_type = db.Column(db.String(30), nullable=False)
    content         = db.Column(db.Text, default='')
    checklist_data  = db.Column(db.Text, default='')
    file_path       = db.Column(db.String(500), default='')
    file_name       = db.Column(db.String(200), default='')
    submitted_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    ai_feedbacks   = db.relationship('AIFeedback', backref='old_submission',
                                     foreign_keys='AIFeedback.submission_id', lazy='dynamic')
    teacher_reviews = db.relationship('TeacherReview', backref='old_submission',
                                      foreign_keys='TeacherReview.submission_id', lazy='dynamic')


# =============================================================================
# 模組 F：訊息系統
# =============================================================================

class Message(db.Model):
    __tablename__ = 'messages'
    id           = db.Column(db.Integer, primary_key=True)
    sender_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    # recipient_id = NULL → 廣播公告；有值 → 私訊

    scope        = db.Column(db.String(20), default='all')
    # 廣播時：'all' | 'class_a' | 'class_b'
    # 私訊時：'personal'

    subject      = db.Column(db.String(200), default='')
    body         = db.Column(db.Text, nullable=False)
    reply_to_id  = db.Column(db.Integer, db.ForeignKey('messages.id'), nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    sender    = db.relationship('User', foreign_keys=[sender_id], backref='sent_messages')
    recipient = db.relationship('User', foreign_keys=[recipient_id], backref='received_messages')
    reads     = db.relationship('MessageRead', backref='message', cascade='all, delete-orphan')


class MessageRead(db.Model):
    __tablename__ = 'message_reads'
    id         = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('messages.id'), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    read_at    = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('message_id', 'user_id'),)


# ─── AI Tutor Conversations ─────────────────────────────────────────────────

class TutorConversation(db.Model):
    __tablename__ = 'tutor_conversations'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    messages   = db.Column(db.Text, default='[]')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship('User', backref='tutor_conversations')


# =============================================================================
# 模組 G：Workshop（v2.5.0 新增）
# 課後自願性工作坊（系統操作/工地參觀/業師講座等），記錄
# 報名 → 出席 → 反思 完整當責行為鏈，作為 EP3「自主 vs 被動」的證據。
# =============================================================================

class Workshop(db.Model):
    __tablename__ = 'workshops'
    id           = db.Column(db.Integer, primary_key=True)
    title        = db.Column(db.String(200), nullable=False)
    type         = db.Column(db.String(30), default='other')
                   # 'system_ops' / 'site_visit' / 'expert_talk' / 'other'
    description  = db.Column(db.Text, default='')
    location     = db.Column(db.String(200), default='')
    starts_at    = db.Column(db.DateTime, nullable=False)
    ends_at      = db.Column(db.DateTime, nullable=False)
    capacity     = db.Column(db.Integer, nullable=True)        # NULL = 無限

    registration_opens_at    = db.Column(db.DateTime, nullable=True)
    registration_closes_at   = db.Column(db.DateTime, nullable=True)
    checkin_code             = db.Column(db.String(10), nullable=False)
    checkin_window_starts_at = db.Column(db.DateTime, nullable=False)
    checkin_window_ends_at   = db.Column(db.DateTime, nullable=False)
    reflection_due_at        = db.Column(db.DateTime, nullable=False)

    semester     = db.Column(db.String(10), nullable=False)    # '114-1'
    status       = db.Column(db.String(20), default='draft')   # draft/published/completed/cancelled
    created_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    participations = db.relationship('WorkshopParticipation', backref='workshop',
                                     cascade='all, delete-orphan', lazy='dynamic')
    creator        = db.relationship('User', foreign_keys=[created_by])


class WorkshopParticipation(db.Model):
    """一名學生對一場工作坊的完整參與紀錄。
    狀態靠 nullable 時間戳記欄位區分：
      未報名         → 整筆不存在
      已報名         → registered_at 有值、cancelled_at 為 NULL
      取消報名       → cancelled_at 有值
      已簽到         → checkin_at 有值
      反思已交       → reflection_submitted_at 有值
    """
    __tablename__ = 'workshop_participations'
    id           = db.Column(db.Integer, primary_key=True)
    workshop_id  = db.Column(db.Integer, db.ForeignKey('workshops.id'), nullable=False)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # 報名階段
    registered_at = db.Column(db.DateTime, nullable=True)
    pre_goal      = db.Column(db.Text, default='')            # 課前自設目標（EP1 證據）
    cancelled_at  = db.Column(db.DateTime, nullable=True)
    cancel_reason = db.Column(db.Text, default='')

    # 出席階段
    checkin_at      = db.Column(db.DateTime, nullable=True)
    checkin_method  = db.Column(db.String(20), default='')    # 'self_code' / 'teacher_manual'

    # 反思階段（3 題）
    reflection_q1           = db.Column(db.Text, default='')  # 目標達成情形
    reflection_q2           = db.Column(db.Text, default='')  # 改變想法的觀察 / 操作
    reflection_q3           = db.Column(db.Text, default='')  # 打算如何應用到後續任務
    reflection_submitted_at = db.Column(db.DateTime, nullable=True)
    willing_to_share        = db.Column(db.Boolean, default=False)  # 預留欄位，MVP 不啟用 UI

    # 後台用
    notes        = db.Column(db.Text, default='')             # 教師備註，學生不可見
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id])

    __table_args__ = (db.UniqueConstraint('workshop_id', 'user_id'),)


# =============================================================================
# v2.5.0 新增
# =============================================================================

class LearningEvent(db.Model):
    """學習行為事件 log，供 EP3 自我監測與 EP4 trigger 細分使用。"""
    __tablename__ = 'learning_events'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    event_type  = db.Column(db.String(50), nullable=False, index=True)
    # ai_feedback_received | ai_feedback_viewed | task_resubmitted | competency_radar_viewed
    entity_type = db.Column(db.String(30), default='')   # 'task_submission' | ''
    entity_id   = db.Column(db.Integer, nullable=True)
    payload_json = db.Column(db.Text, default='')
    created_at  = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship('User', foreign_keys=[user_id])


class TaskSubmissionSnapshot(db.Model):
    """任務提交的版本快照，記錄每次重交當下的內容與 AI 分數。"""
    __tablename__ = 'task_submission_snapshots'
    id                  = db.Column(db.Integer, primary_key=True)
    task_submission_id  = db.Column(db.Integer, db.ForeignKey('task_submissions.id'),
                                    nullable=False, index=True)
    revision_number     = db.Column(db.Integer, nullable=False)
    trigger             = db.Column(db.String(50), nullable=False)
    # submitted_initial | ai_feedback_attached |
    # submitted_after_feedback | resubmitted_without_feedback_view
    payload_json        = db.Column(db.Text, nullable=False)  # 當下內容 + AI 分數
    last_ai_feedback_id = db.Column(db.Integer, db.ForeignKey('ai_feedbacks.id'), nullable=True)
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)


class TaskSchedule(db.Model):
    """任務日期權威來源。每次調整寫新 row，最新 effective_from <= now() 為有效值。"""
    __tablename__ = 'task_schedules'
    id             = db.Column(db.Integer, primary_key=True)
    task_number    = db.Column(db.Integer, nullable=False, index=True)
    opens_at       = db.Column(db.DateTime, nullable=False)
    deadline_at    = db.Column(db.DateTime, nullable=False)
    effective_from = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    set_by         = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    date_source    = db.Column(db.String(50), default='')
    # 'retrospective_from_syllabus_YYYY-MM-DD' | 'teacher_defined_YYYY-MM-DD' | 'syllabus_default'
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)


class TaskDateChangeLog(db.Model):
    """任務日期變更歷程，純 audit log，不作為有效值來源。"""
    __tablename__ = 'task_date_change_logs'
    id          = db.Column(db.Integer, primary_key=True)
    task_number = db.Column(db.Integer, nullable=False, index=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey('task_schedules.id'), nullable=False)
    field_name  = db.Column(db.String(20), nullable=False)
    old_value   = db.Column(db.String(40), default='')
    new_value   = db.Column(db.String(40), nullable=False)
    changed_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reason      = db.Column(db.Text, default='')
    changed_at  = db.Column(db.DateTime, default=datetime.utcnow)


class SelfStudyProposal(db.Model):
    """對照組自主學習提案（提案→核准→執行→成果→評閱 完整流程）。"""
    __tablename__ = 'self_study_proposals'
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    proposal_number = db.Column(db.Integer, nullable=False)  # 1–4
    semester        = db.Column(db.String(10), nullable=False)

    # 提案
    topic           = db.Column(db.String(200), default='')
    motivation      = db.Column(db.Text, default='')
    expected_output = db.Column(db.Text, default='')
    schedule        = db.Column(db.Text, default='')
    proposed_at     = db.Column(db.DateTime, nullable=True)

    # 核准
    approval_status      = db.Column(db.String(20), default='draft')
    # draft | submitted | approved | revise_needed | result_submitted | finalized | overdue
    teacher_comment      = db.Column(db.Text, default='')
    reviewed_by          = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_at          = db.Column(db.DateTime, nullable=True)
    proposal_deadline_at = db.Column(db.DateTime, nullable=True)
    result_deadline_at   = db.Column(db.DateTime, nullable=True)

    # 成果
    result_content      = db.Column(db.Text, default='')
    result_file_path    = db.Column(db.String(500), default='')
    result_file_name    = db.Column(db.String(200), default='')
    reflection          = db.Column(db.Text, default='')
    result_submitted_at = db.Column(db.DateTime, nullable=True)

    # 評閱
    final_score    = db.Column(db.Float, nullable=True)
    final_feedback = db.Column(db.Text, default='')
    rubric_json    = db.Column(db.Text, default='')
    reviewer_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    finalized_at   = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'proposal_number', 'semester'),)


class OralPresentationAssessment(db.Model):
    """期末口頭報告教師現場觀察評分。DP5 正式 performance 資料源，兩組共用。"""
    __tablename__ = 'oral_presentation_assessments'
    id       = db.Column(db.Integer, primary_key=True)
    user_id  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    semester = db.Column(db.String(10), nullable=False)

    # 4 個向度（1–5 分），nullable 直到教師評分
    score_content   = db.Column(db.Integer, nullable=True)  # 專業內容正確性
    score_structure = db.Column(db.Integer, nullable=True)  # 報告結構清楚度
    score_delivery  = db.Column(db.Integer, nullable=True)  # 口語表達與時間掌控
    score_qa        = db.Column(db.Integer, nullable=True)  # 問題回應與當責說明

    teacher_comment   = db.Column(db.Text, default='')
    reviewer_id       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    finalized_at      = db.Column(db.DateTime, nullable=True)  # 教師按「確認」才寫入

    presentation_date  = db.Column(db.DateTime, nullable=True)
    supplementary_url  = db.Column(db.String(500), default='')
    supplementary_note = db.Column(db.Text, default='')

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'semester'),)
