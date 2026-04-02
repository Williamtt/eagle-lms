import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

    # Database: use PostgreSQL on Railway, SQLite locally
    DATABASE_URL = os.environ.get('DATABASE_URL', '')
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_DATABASE_URI = DATABASE_URL or 'sqlite:///eagle_lms.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # File uploads
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'static/uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max

    # AI
    ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

    # Teacher credentials
    TEACHER_CODE  = os.environ.get('TEACHER_CODE', 'eagle2025')

    # Email notifications (leave blank to disable)
    TEACHER_EMAIL = os.environ.get('TEACHER_EMAIL', '')
    SMTP_HOST     = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
    SMTP_PORT     = int(os.environ.get('SMTP_PORT', '587'))
    SMTP_USER     = os.environ.get('SMTP_USER', '')
    SMTP_PASS     = os.environ.get('SMTP_PASS', '')
