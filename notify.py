"""
Email notification utility for EAGLE LMS.
All functions silently fail if SMTP is not configured — notifications are non-critical.
"""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def _send(subject: str, body: str, cfg: dict) -> None:
    teacher_email = cfg.get('TEACHER_EMAIL', '')
    smtp_host     = cfg.get('SMTP_HOST', '')
    smtp_user     = cfg.get('SMTP_USER', '')
    smtp_pass     = cfg.get('SMTP_PASS', '')
    smtp_port     = int(cfg.get('SMTP_PORT', 587))

    if not all([teacher_email, smtp_host, smtp_user, smtp_pass]):
        return  # Not configured — skip silently

    try:
        msg = MIMEMultipart('alternative')
        msg['From']    = smtp_user
        msg['To']      = teacher_email
        msg['Subject'] = f'[EAGLE LMS] {subject}'
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
    except Exception:
        pass  # Notification failure must never break the main flow


def notify_new_registration(student_name: str, student_id: str,
                             class_group: str, cfg: dict) -> None:
    subject = f'新學生待審核：{student_name}（{student_id}）'
    body = (
        f'有新學生完成 EAGLE LMS 註冊，請登入教師後台進行審核。\n\n'
        f'姓名：{student_name}\n'
        f'學號：{student_id}\n'
        f'班級：{class_group}\n\n'
        f'請至教師儀表板 → 「待審核帳號」完成審核。'
    )
    _send(subject, body, cfg)


def notify_new_submission(student_name: str, student_id: str,
                           task_number: int, cfg: dict) -> None:
    subject = f'新任務提交待評閱：任務 {task_number}（{student_name}）'
    body = (
        f'{student_name}（{student_id}）剛提交了任務 {task_number}，請登入教師後台進行評閱。\n\n'
        f'請至教師儀表板 → 任務 {task_number} → 查看提交列表。'
    )
    _send(subject, body, cfg)
