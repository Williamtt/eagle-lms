import os

_DEFAULT_SECRET_KEY  = 'dev-secret-key-change-in-production'
_DEFAULT_TEACHER_CODE = 'eagle2025'


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', _DEFAULT_SECRET_KEY)

    # Database: use PostgreSQL on Railway, SQLite locally
    DATABASE_URL = os.environ.get('DATABASE_URL', '')
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_DATABASE_URI = DATABASE_URL or 'sqlite:///eagle_lms.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # File uploads
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'static/uploads')
    MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # 32MB max

    # AI
    ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

    # Teacher credentials
    TEACHER_CODE  = os.environ.get('TEACHER_CODE', _DEFAULT_TEACHER_CODE)

    # Fail-fast in production-like deploys (any non-SQLite DB → require real secrets)
    _is_remote_db = bool(DATABASE_URL) and not SQLALCHEMY_DATABASE_URI.startswith('sqlite')
    if _is_remote_db:
        if SECRET_KEY == _DEFAULT_SECRET_KEY:
            raise RuntimeError(
                'SECRET_KEY env var is required when DATABASE_URL points to a remote database.'
            )
        if TEACHER_CODE == _DEFAULT_TEACHER_CODE:
            raise RuntimeError(
                'TEACHER_CODE env var is required when DATABASE_URL points to a remote database.'
            )

    # AI Tutor Service
    AI_TUTOR_URL = os.environ.get('AI_TUTOR_URL', 'http://localhost:8000')
    AI_TUTOR_SERVICE_KEY = os.environ.get('AI_TUTOR_SERVICE_KEY', '')

    # Email notifications (leave blank to disable)
    TEACHER_EMAIL = os.environ.get('TEACHER_EMAIL', '')
    SMTP_HOST     = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
    SMTP_PORT     = int(os.environ.get('SMTP_PORT', '587'))
    SMTP_USER     = os.environ.get('SMTP_USER', '')
    SMTP_PASS     = os.environ.get('SMTP_PASS', '')
