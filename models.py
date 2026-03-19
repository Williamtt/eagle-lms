from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(10), default='student')  # student / teacher
    class_group = db.Column(db.String(10), default='A')  # A / B
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    submissions = db.relationship('Submission', backref='author', lazy='dynamic')
    reviews = db.relationship('TeacherReview', backref='reviewer', lazy='dynamic',
                              foreign_keys='TeacherReview.teacher_id')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_teacher(self):
        return self.role == 'teacher'


class Submission(db.Model):
    __tablename__ = 'submissions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    task_number = db.Column(db.Integer, nullable=False)  # 1-4

    # Submission type: reflection, question, checklist, self_assessment
    submission_type = db.Column(db.String(30), nullable=False)
    content = db.Column(db.Text, default='')

    # For checklist/self-assessment: JSON data
    checklist_data = db.Column(db.Text, default='')

    # File upload
    file_path = db.Column(db.String(500), default='')
    file_name = db.Column(db.String(200), default='')

    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    ai_feedbacks = db.relationship('AIFeedback', backref='submission', lazy='dynamic')
    teacher_reviews = db.relationship('TeacherReview', backref='submission', lazy='dynamic')


class AIFeedback(db.Model):
    __tablename__ = 'ai_feedbacks'
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey('submissions.id'), nullable=False)

    feedback = db.Column(db.Text, nullable=False)
    scores = db.Column(db.Text, default='')  # JSON: {completeness, accuracy, reflection, ...}
    model_used = db.Column(db.String(50), default='claude-sonnet-4-20250514')

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TeacherReview(db.Model):
    __tablename__ = 'teacher_reviews'
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey('submissions.id'), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    feedback = db.Column(db.Text, default='')
    score = db.Column(db.Float, nullable=True)
    published = db.Column(db.Boolean, default=False)  # visible to student?

    reviewed_at = db.Column(db.DateTime, default=datetime.utcnow)
