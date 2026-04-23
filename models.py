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
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    consent_agreed_at = db.Column(db.DateTime, nullable=True)    # 學生同意知情同意書的時間戳記

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
    checked       = db.Column(db.Boolean, default=False)
    note          = db.Column(db.Text, default='')            # 補充說明（選填）


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
