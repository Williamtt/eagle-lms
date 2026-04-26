import os
import csv
import io
import json
import secrets
from functools import wraps
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
from flask import (Flask, render_template, request, redirect,
                   url_for, flash, send_from_directory, jsonify,
                   Response, abort, session)
from flask_login import (LoginManager, login_user, logout_user,
                         login_required, current_user)
from werkzeug.utils import secure_filename

from config import Config
from models import (db, User,
                    TaskSubmission, QuestionResponse, ChecklistResponse,
                    ReflectionResponse, DeliverableUpload,
                    AIFeedback, TeacherReview, AIReviewSuggestion,
                    Questionnaire, QuestionnaireItem,
                    QuestionnaireSubmission, QuestionnaireAnswer,
                    LearningJournal,
                    Submission,          # Submission 保留供舊資料查詢
                    TutorConversation,
                    Message, MessageRead,
                    Workshop, WorkshopParticipation,
                    LearningEvent, TaskSubmissionSnapshot,
                    TaskSchedule, TaskDateChangeLog,
                    SelfStudyProposal, OralPresentationAssessment)
from sqlalchemy import or_, and_
import requests as http_requests
import ai_service
import notify
from task_definitions import (TASKS, SEMESTER, SYSTEM_VERSION, LEARNING_JOURNALS,
                              CONTROL_GROUP_TASKS, AXES_DESCRIPTIONS)

# 允許的班級選項（學生註冊用）
VALID_CLASS_GROUPS = ['營建管理A班', '營建管理B班']

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)


def _run_migrations():
    """Add new columns to existing tables without Flask-Migrate."""
    from sqlalchemy import inspect, text
    with app.app_context():
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()

        def cols(table): return [c['name'] for c in inspector.get_columns(table)]

        with db.engine.connect() as conn:
            # ── users ──
            if 'users' in tables:
                u = cols('users')
                if 'status' not in u:
                    conn.execute(text("ALTER TABLE users ADD COLUMN status VARCHAR(10) DEFAULT 'active'"))
                if 'consent_agreed_at' not in u:
                    conn.execute(text("ALTER TABLE users ADD COLUMN consent_agreed_at TIMESTAMP"))
                if 'reset_requested_at' not in u:
                    conn.execute(text("ALTER TABLE users ADD COLUMN reset_requested_at TIMESTAMP"))
                if 'reset_contact_email' not in u:
                    conn.execute(text("ALTER TABLE users ADD COLUMN reset_contact_email VARCHAR(120)"))
                if 'experimental_group' not in u:
                    conn.execute(text("ALTER TABLE users ADD COLUMN experimental_group VARCHAR(20)"))

            # ── teacher_reviews ──
            if 'teacher_reviews' in tables:
                tr = cols('teacher_reviews')
                if 'rubric_json' not in tr:
                    conn.execute(text("ALTER TABLE teacher_reviews ADD COLUMN rubric_json TEXT DEFAULT ''"))
                if 'rubric_finalized_at' not in tr:
                    conn.execute(text("ALTER TABLE teacher_reviews ADD COLUMN rubric_finalized_at TIMESTAMP"))
                if 'rubric_source' not in tr:
                    conn.execute(text("ALTER TABLE teacher_reviews ADD COLUMN rubric_source VARCHAR(40) DEFAULT ''"))

            # ── learning_journals ──
            if 'learning_journals' in tables:
                lj = cols('learning_journals')
                if 'evaluation_json' not in lj:
                    conn.execute(text("ALTER TABLE learning_journals ADD COLUMN evaluation_json TEXT DEFAULT ''"))

            conn.commit()

        # 新表（包含 v2.5.0 Workshop + 所有 v2.5.0 新增表）
        db.create_all()


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = '請先登入。'

ALLOWED_EXTENSIONS = {'pdf', 'xlsx', 'xls', 'docx', 'doc',
                      'png', 'jpg', 'jpeg', 'zip'}


# ─── 簡易 CSRF 防護（只給高風險教師路由用） ──────────────────────────────────
# 全站 CSRF 後續再上 Flask-WTF；這裡僅針對破壞性教師操作做最低限度防護。
def _get_csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


def csrf_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        sent = request.form.get('_csrf') or request.headers.get('X-CSRFToken', '')
        expected = session.get('_csrf_token', '')
        if not expected or not secrets.compare_digest(sent, expected):
            abort(400, 'CSRF token invalid or missing.')
        return view(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_csrf_token():
    return {'csrf_token': _get_csrf_token}


def _student_scope_key(user):
    return 'class_a' if user.class_group == '營建管理A班' else 'class_b'


def _student_msg_filter(user):
    scope_key = _student_scope_key(user)
    return or_(
        Message.recipient_id == user.id,
        and_(
            Message.recipient_id == None,
            or_(Message.scope == 'all', Message.scope == scope_key)
        )
    )


@app.context_processor
def inject_unread_count():
    if not current_user.is_authenticated:
        return {'unread_count': 0, 'reset_request_count': 0, 'proposal_pending_count': 0}
    read_ids = db.session.query(MessageRead.message_id).filter_by(user_id=current_user.id)
    if current_user.is_teacher:
        msg_count = Message.query.filter(
            Message.recipient_id == current_user.id,
            Message.id.notin_(read_ids)
        ).count()
        reset_count = User.query.filter(
            User.role == 'student',
            User.reset_requested_at != None
        ).count()
        proposal_pending = SelfStudyProposal.query.filter(
            SelfStudyProposal.approval_status.in_(['submitted', 'result_submitted'])
        ).count()
        return {'unread_count': msg_count, 'reset_request_count': reset_count,
                'proposal_pending_count': proposal_pending}
    else:
        count = Message.query.filter(
            Message.id.notin_(read_ids),
            _student_msg_filter(current_user)
        ).count()
        return {'unread_count': count, 'reset_request_count': 0, 'proposal_pending_count': 0}

# Jinja2 custom filter：將 JSON 字串解析為 Python 物件（供問卷選項使用）
@app.template_filter('from_json')
def from_json_filter(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return []


def allowed_file(filename):
    return ('.' in filename and
            filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ─── Auth Routes ──────────────────────────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        import re
        student_id   = request.form.get('student_id', '').strip()
        name         = request.form.get('name', '').strip()
        password     = request.form.get('password', '')
        confirm      = request.form.get('confirm_password', '')
        class_group  = request.form.get('class_group', '').strip()
        teacher_code = request.form.get('teacher_code', '').strip()

        is_teacher = (teacher_code == app.config['TEACHER_CODE'])
        consent_agreed = request.form.get('consent_agreed')

        # 必填欄位
        if not student_id or not name or not password:
            flash('請填寫所有必填欄位。', 'error')
            return render_template('register.html', valid_class_groups=VALID_CLASS_GROUPS)

        # 學生必須同意知情同意書
        if not is_teacher and not consent_agreed:
            flash('請先閱讀並勾選同意研究參與者知情同意書，才能完成註冊。', 'error')
            return render_template('register.html', valid_class_groups=VALID_CLASS_GROUPS)

        # 學號格式：9碼純數字（教師不限制）
        if not is_teacher and not re.fullmatch(r'\d{9}', student_id):
            flash('學號格式錯誤，請輸入 9 碼數字（例如：409380572）。', 'error')
            return render_template('register.html', valid_class_groups=VALID_CLASS_GROUPS)

        # 班級：學生必須選擇有效班級
        if not is_teacher and class_group not in VALID_CLASS_GROUPS:
            flash('請選擇正確的班級。', 'error')
            return render_template('register.html', valid_class_groups=VALID_CLASS_GROUPS)

        if password != confirm:
            flash('兩次密碼不一致。', 'error')
            return render_template('register.html', valid_class_groups=VALID_CLASS_GROUPS)
        if len(password) < 6:
            flash('密碼至少需要 6 個字元。', 'error')
            return render_template('register.html', valid_class_groups=VALID_CLASS_GROUPS)
        if User.query.filter_by(student_id=student_id).first():
            flash('此學號已被註冊。', 'error')
            return render_template('register.html', valid_class_groups=VALID_CLASS_GROUPS)

        role   = 'teacher' if is_teacher else 'student'
        status = 'active'  if is_teacher else 'pending'
        cg     = class_group if class_group else '未分班'

        user = User(student_id=student_id, name=name,
                    role=role, class_group=cg, status=status,
                    consent_agreed_at=datetime.utcnow() if not is_teacher else None)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        if role == 'student':
            notify.notify_new_registration(name, student_id, cg, app.config)
            flash('註冊申請已送出！請等待教師審核後即可登入。', 'success')
        else:
            flash('教師帳號建立成功！請登入。', 'success')
        return redirect(url_for('login'))

    return render_template('register.html', valid_class_groups=VALID_CLASS_GROUPS)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        student_id = request.form.get('student_id', '').strip()
        password   = request.form.get('password', '')
        user = User.query.filter_by(student_id=student_id).first()
        if user and user.check_password(password):
            if user.status == 'pending':
                flash('帳號尚待教師審核，審核通過後即可登入。', 'warning')
                return render_template('login.html')
            if user.status == 'disabled':
                flash('帳號已停用，請聯繫教師。', 'error')
                return render_template('login.html')
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('學號或密碼錯誤。', 'error')
    return render_template('login.html')


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        student_id = request.form.get('student_id', '').strip()
        contact_email = request.form.get('contact_email', '').strip()
        user = User.query.filter_by(student_id=student_id, role='student').first()
        if not user:
            flash('找不到此學號，請確認後再試。', 'error')
            return render_template('forgot_password.html')
        user.reset_requested_at  = datetime.utcnow()
        user.reset_contact_email = contact_email or None
        db.session.commit()
        notify.notify_forgot_password(user.name, user.student_id, contact_email, app.config)
        flash('已通知教師，請等候老師重設密碼後告知您。', 'success')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


# ─── Static Pages ─────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/manual')
@login_required
def manual():
    if not current_user.is_teacher and current_user.experimental_group != 'experimental':
        flash('任務手冊僅開放給實驗組學生。', 'info')
        return redirect(url_for('dashboard'))
    return render_template('manual.html',
                           system_version=SYSTEM_VERSION,
                           semester=SEMESTER)


# ─── Student: Dashboard ───────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.is_teacher:
        return redirect(url_for('teacher_dashboard'))

    # 每個任務：讀取該學生本學期唯一的 TaskSubmission
    task_status = {}
    for t_num, t_def in TASKS.items():
        sub = TaskSubmission.query.filter_by(
            user_id=current_user.id,
            task_number=t_num,
            semester=SEMESTER
        ).first()

        reviewed = False
        if sub:
            reviewed = sub.teacher_reviews.filter_by(
                published=True).first() is not None

        is_draft = sub is not None and sub.status == 'draft'
        task_status[t_num] = {
            'name':          t_def['name'],
            'week_range':    t_def['week_range'],
            'submitted':     sub is not None and not is_draft,
            'is_draft':      is_draft,
            'reviewed':      reviewed,
            'submission_id': sub.id if sub else None,
            'updated_at':    sub.updated_at if sub else None,
        }

    # 學習日誌狀態
    journal_status = {}
    for j_def in LEARNING_JOURNALS:
        j_num = j_def['journal_number']
        j_def_group = _journal_for_group(j_def, current_user.experimental_group)
        journal = LearningJournal.query.filter_by(
            user_id=current_user.id,
            journal_number=j_num,
            semester=SEMESTER
        ).first()
        journal_status[j_num] = {
            'title':      j_def_group['title'],
            'week':       j_def['week'],
            'due_date':   j_def.get('due_date', ''),
            'submitted':  journal is not None,
            'journal_id': journal.id if journal else None,
        }

    # 開放中的問卷
    active_questionnaires = Questionnaire.query.filter_by(
        is_active=True, semester=SEMESTER).all()
    completed_q_codes = set()
    for q in active_questionnaires:
        if QuestionnaireSubmission.query.filter_by(
                user_id=current_user.id,
                questionnaire_id=q.id).first():
            completed_q_codes.add(q.code)

    # 前測問卷提醒：arcsa_pre 已開放且學生尚未完成
    pre_test_pending = False
    pre_q = Questionnaire.query.filter_by(code='arcsa_pre', is_active=True).first()
    if pre_q:
        pre_test_pending = not QuestionnaireSubmission.query.filter_by(
            user_id=current_user.id,
            questionnaire_id=pre_q.id
        ).first()

    # 工作坊區塊：未來 7 天內或進行中、且 status=published 的工作坊（最多 3 場）
    # + 個人提醒（已簽到但未交反思 / 已報名即將開始）
    now = _now()
    upcoming_workshops_q = Workshop.query.filter(
        Workshop.semester == SEMESTER,
        Workshop.status == 'published',
        Workshop.ends_at >= now,
    ).order_by(Workshop.starts_at.asc()).limit(3).all()
    parts_map = {p.workshop_id: p for p in WorkshopParticipation.query.filter_by(
        user_id=current_user.id).all()}
    upcoming_workshops = []
    for w in upcoming_workshops_q:
        p = parts_map.get(w.id)
        upcoming_workshops.append({
            'workshop': w,
            'status':   _workshop_status_for(p, w, now),
        })

    # 提醒：已簽到但反思未交且未過期
    reflection_pending = []
    for p in parts_map.values():
        w = p.workshop
        if p.checkin_at and not p.reflection_submitted_at \
                and now <= w.reflection_due_at:
            reflection_pending.append({'workshop': w, 'participation': p})

    # 對照組：查詢自主學習提案
    self_study_proposals = []
    if current_user.experimental_group == 'control':
        self_study_proposals = SelfStudyProposal.query.filter_by(
            user_id=current_user.id, semester=SEMESTER
        ).order_by(SelfStudyProposal.proposal_number).all()

    return render_template('student/dashboard.html',
                           task_status=task_status,
                           journal_status=journal_status,
                           active_questionnaires=active_questionnaires,
                           completed_q_codes=completed_q_codes,
                           pre_test_pending=pre_test_pending,
                           upcoming_workshops=upcoming_workshops,
                           reflection_pending=reflection_pending,
                           workshop_type_labels=WORKSHOP_TYPE_LABELS,
                           tasks=TASKS,
                           self_study_proposals=self_study_proposals,
                           ctrl_tasks=CONTROL_GROUP_TASKS)


# ─── Student: Task & Structured Submission ────────────────────────────────────

@app.route('/task/<int:task_number>')
@login_required
def view_task(task_number):
    if current_user.experimental_group == 'control':
        flash('此功能僅開放給實驗組學生。', 'error')
        return redirect(url_for('dashboard'))
    task_def = TASKS.get(task_number)
    if not task_def:
        flash('無效的任務編號。', 'error')
        return redirect(url_for('dashboard'))

    # 取出既有提交（本學期唯一一筆）
    existing_sub = TaskSubmission.query.filter_by(
        user_id=current_user.id,
        task_number=task_number,
        semester=SEMESTER
    ).first()

    # 整理預填資料供 template 使用
    # 鍵值格式與 form field name 前綴對應：
    #   pq  → name="pq_{question_id}"
    #   cl  → name="cl_{item_id}"  + name="cl_note_{item_id}"
    #   rq  → name="rq_{question_id}"
    #   dv_text / dv_file → name="dv_text_{deliverable_id}" / name="dv_file_{deliverable_id}"
    existing_data = {
        'pq':      {},   # question_id  → answer (str)
        'cl':      {},   # item_id      → {'checked': bool, 'note': str}
        'rq':      {},   # question_id  → answer (str)
        'dv_text': {},   # deliverable_id → content (str)
        'dv_file': {},   # deliverable_id → file_name (str)
    }

    if existing_sub:
        for qr in existing_sub.question_responses:
            existing_data['pq'][qr.question_id] = qr.answer
        for cr in existing_sub.checklist_responses:
            existing_data['cl'][cr.item_id] = {
                'checked': cr.checked, 'note': cr.note
            }
        for rr in existing_sub.reflection_responses:
            existing_data['rq'][rr.question_id] = rr.answer
        for du in existing_sub.deliverable_uploads:
            existing_data['dv_text'][du.deliverable_id] = du.content
            existing_data['dv_file'][du.deliverable_id] = du.file_name

    ai_fb = None
    teacher_review = None
    if existing_sub:
        ai_fb = existing_sub.ai_feedbacks.order_by(
            AIFeedback.created_at.desc()).first()
        teacher_review = existing_sub.teacher_reviews.filter_by(
            published=True).order_by(TeacherReview.reviewed_at.desc()).first()

    return render_template('student/task.html',
                           task_number=task_number,
                           task_def=task_def,
                           existing_sub=existing_sub,
                           existing_data=existing_data,
                           ai_feedback=ai_fb,
                           teacher_review=teacher_review)


@app.route('/submit/<int:task_number>', methods=['POST'])
@login_required
def submit_task(task_number):
    if current_user.experimental_group == 'control':
        flash('此功能僅開放給實驗組學生。', 'error')
        return redirect(url_for('dashboard'))
    task_def = TASKS.get(task_number)
    if not task_def:
        flash('無效的任務編號。', 'error')
        return redirect(url_for('dashboard'))

    # ── 取得或建立 TaskSubmission ──────────────────────────────────────────────
    sub = TaskSubmission.query.filter_by(
        user_id=current_user.id,
        task_number=task_number,
        semester=SEMESTER
    ).first()

    # 已評閱的任務不可再提交
    if sub and sub.status == 'reviewed':
        flash('此任務已完成評閱，不可再修改或重新提交。', 'error')
        return redirect(url_for('view_task', task_number=task_number))

    is_update = sub is not None

    if is_update:
        # 清除可重寫的子記錄（文字型）；DeliverableUpload 另行 upsert 以保留舊檔案
        QuestionResponse.query.filter_by(submission_id=sub.id).delete()
        ChecklistResponse.query.filter_by(submission_id=sub.id).delete()
        ReflectionResponse.query.filter_by(submission_id=sub.id).delete()
        sub.task_version = task_def['version']
        sub.updated_at   = datetime.utcnow()
    else:
        sub = TaskSubmission(
            user_id      = current_user.id,
            task_number  = task_number,
            task_version = task_def['version'],
            semester     = SEMESTER,
        )
        db.session.add(sub)
        db.session.flush()   # 確保拿到 sub.id

    # ── 提示問題回答（form field: pq_{question_id}）────────────────────────────
    for pq in task_def['prompt_questions']:
        answer = request.form.get(f'pq_{pq["id"]}', '').strip()
        db.session.add(QuestionResponse(
            submission_id=sub.id,
            question_id=pq['id'],    # e.g. "t1_pq1"
            answer=answer
        ))

    # ── 自我檢核（form field: cl_{item_id}  +  cl_note_{item_id}）───────────────
    for cl in task_def['checklist_items']:
        checked = request.form.get(f'cl_{cl["id"]}') == 'on'
        note    = request.form.get(f'cl_note_{cl["id"]}', '').strip()
        db.session.add(ChecklistResponse(
            submission_id=sub.id,
            item_id=cl['id'],        # e.g. "t1_cl1"
            checked=checked,
            note=note
        ))

    # ── 當責反思（form field: rq_{question_id}）────────────────────────────────
    for rq in task_def['reflection_questions']:
        answer = request.form.get(f'rq_{rq["id"]}', '').strip()
        db.session.add(ReflectionResponse(
            submission_id=sub.id,
            question_id=rq['id'],    # e.g. "t1_rq1"
            answer=answer
        ))

    # ── 任務產出（form field: dv_text_{deliverable_id}  +  dv_file_{deliverable_id}）
    upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], str(current_user.id))
    os.makedirs(upload_dir, exist_ok=True)

    for dv in task_def['deliverables']:
        d_id = dv['id']   # e.g. "t1_d1"

        # 文字內容
        content = ''
        if dv.get('accept_text'):
            content = request.form.get(f'dv_text_{d_id}', '').strip()

        # 檔案上傳
        new_file_path = ''
        new_file_name = ''
        if dv.get('accept_file'):
            file = request.files.get(f'dv_file_{d_id}')
            if file and file.filename and allowed_file(file.filename):
                safe_name = secure_filename(
                    f"{current_user.student_id}_t{task_number}_{d_id}_{file.filename}"
                )
                fpath = os.path.join(upload_dir, safe_name)
                file.save(fpath)
                new_file_path = fpath
                new_file_name = file.filename

        # Upsert DeliverableUpload（保留舊檔案，若本次未上傳新檔）
        du = DeliverableUpload.query.filter_by(
            submission_id=sub.id,
            deliverable_id=d_id
        ).first()
        if du:
            du.content = content
            if new_file_path:
                du.file_path = new_file_path
                du.file_name = new_file_name
        else:
            db.session.add(DeliverableUpload(
                submission_id  = sub.id,
                deliverable_id = d_id,
                content        = content,
                file_path      = new_file_path,
                file_name      = new_file_name,
            ))

    # ── 判斷暫存或正式提交 ────────────────────────────────────────────────────
    submit_action = request.form.get('submit_action', 'submit')
    is_draft = (submit_action == 'draft')
    sub.status = 'draft' if is_draft else 'submitted'

    db.session.commit()

    if is_draft:
        flash('草稿已暫存。你可以隨時回來繼續編輯，完成後請記得點「正式提交」。', 'info')
        return redirect(url_for('view_task', task_number=task_number))

    # ── Snapshot 1：首次提交或重交 trigger ────────────────────────────────────
    from services.competency import determine_resubmit_trigger, _snapshot_submission
    if is_update:
        resubmit_trigger = determine_resubmit_trigger(sub.id, current_user.id)
        _snapshot_submission(sub, trigger=resubmit_trigger)
        db.session.add(LearningEvent(
            user_id      = current_user.id,
            event_type   = 'task_resubmitted',
            entity_type  = 'task_submission',
            entity_id    = sub.id,
            payload_json = json.dumps({'trigger': resubmit_trigger}, ensure_ascii=False),
        ))
    else:
        _snapshot_submission(sub, trigger='submitted_initial')

    # ── AI 整體回饋 ────────────────────────────────────────────────────────────
    if app.config.get('ANTHROPIC_API_KEY'):
        # 整合所有文字回答供 AI 評閱
        submission_text = _build_submission_text_for_ai(sub, task_def)
        result = ai_service.generate_instant_feedback(
            task_number,
            'structured',
            submission_text,
            current_user.name
        )
        ai_fb = AIFeedback(
            task_submission_id = sub.id,
            feedback_type      = 'overall',
            feedback           = result.get('feedback', ''),
            scores             = json.dumps(
                result.get('scores', {}), ensure_ascii=False),
            model_used         = 'claude-sonnet-4-20250514'
        )
        db.session.add(ai_fb)
        db.session.flush()  # 取得 ai_fb.id
        db.session.add(LearningEvent(
            user_id      = current_user.id,
            event_type   = 'ai_feedback_received',
            entity_type  = 'task_submission',
            entity_id    = sub.id,
            payload_json = json.dumps({'ai_feedback_id': ai_fb.id}, ensure_ascii=False),
        ))
        # Snapshot 2：AI 回饋附加完成
        _snapshot_submission(sub, trigger='ai_feedback_attached', ai_feedback_id=ai_fb.id)
        db.session.commit()
        flash('提交成功！AI 助教已提供初步回饋。', 'success')
    else:
        db.session.commit()  # 確保 Snapshot 1 也寫入
        flash('提交成功！', 'success')

    notify.notify_new_submission(
        current_user.name, current_user.student_id, task_number, app.config
    )
    return redirect(url_for('view_task', task_number=task_number))


def _build_submission_text_for_ai(sub, task_def):
    """將結構化回答組合成 AI 可評閱的文字段落"""
    parts = []

    # 提示問題
    pq_map = {r.question_id: r.answer
              for r in sub.question_responses}
    if pq_map:
        parts.append('【提示問題回答】')
        for pq in task_def['prompt_questions']:
            ans = pq_map.get(pq['id'], '（未作答）')
            parts.append(f"Q: {pq['text']}\nA: {ans}")

    # 自我檢核
    cl_map = {r.item_id: r for r in sub.checklist_responses}
    if cl_map:
        parts.append('\n【自我檢核】')
        for cl in task_def['checklist_items']:
            cr = cl_map.get(cl['id'])
            status = '✓' if (cr and cr.checked) else '✗'
            note = f'（{cr.note}）' if (cr and cr.note) else ''
            parts.append(f"{status} {cl['text']}{note}")

    # 當責反思
    rq_map = {r.question_id: r.answer
              for r in sub.reflection_responses}
    if rq_map:
        parts.append('\n【當責反思】')
        for rq in task_def['reflection_questions']:
            ans = rq_map.get(rq['id'], '（未作答）')
            parts.append(f"Q: {rq['text']}\nA: {ans}")

    # 反思報告（deliverable 中的文字產出）
    du_map = {du.deliverable_id: du.content
              for du in sub.deliverable_uploads if du.content}
    if du_map:
        parts.append('\n【任務產出（文字）】')
        for dv in task_def['deliverables']:
            if dv.get('accept_text') and dv['id'] in du_map:
                parts.append(f"{dv['label']}：\n{du_map[dv['id']]}")

    return '\n\n'.join(parts)


# ─── Student: Task Submission Detail ──────────────────────────────────────────

@app.route('/task-submission/<int:submission_id>')
@login_required
def view_task_submission(submission_id):
    sub = db.session.get(TaskSubmission, submission_id)
    if not sub:
        flash('找不到此提交。', 'error')
        return redirect(url_for('dashboard'))
    if not current_user.is_teacher and sub.user_id != current_user.id:
        flash('無權限查看。', 'error')
        return redirect(url_for('dashboard'))

    task_def = TASKS.get(sub.task_number, {})

    # 整理成 dict 供 template 查找
    pq_map  = {r.question_id: r.answer for r in sub.question_responses}
    cl_map  = {r.item_id: r            for r in sub.checklist_responses}
    rq_map  = {r.question_id: r.answer for r in sub.reflection_responses}
    du_map  = {du.deliverable_id: du   for du in sub.deliverable_uploads}

    ai_fb = sub.ai_feedbacks.order_by(
        AIFeedback.created_at.desc()).first()
    teacher_review = sub.teacher_reviews.filter_by(published=True)\
        .order_by(TeacherReview.reviewed_at.desc()).first()
    if not current_user.is_teacher and teacher_review and not teacher_review.published:
        teacher_review = None

    return render_template('student/task_submission_detail.html',
                           sub=sub,
                           task_def=task_def,
                           pq_map=pq_map,
                           cl_map=cl_map,
                           rq_map=rq_map,
                           du_map=du_map,
                           ai_feedback=ai_fb,
                           teacher_review=teacher_review)


# ─── 對照組自主學習 ────────────────────────────────────────────────────────────

# 每個提案編號對應的評量向度與最低證據要求（教師核准時檢核）
def _proposal_axes(proposal_number):
    """Return scoring axes for a given proposal_number (from CONTROL_GROUP_TASKS)."""
    return CONTROL_GROUP_TASKS.get(proposal_number, {}).get('axes', ['DP1', 'DP2', 'DP3', 'DP4'])


@app.route('/self-study')
@login_required
def self_study_list():
    if current_user.is_teacher:
        return redirect(url_for('teacher_self_study_list'))
    if current_user.experimental_group != 'control':
        flash('此功能僅開放給對照組學生。', 'error')
        return redirect(url_for('dashboard'))

    # 防呆：若有缺漏則補建固定 4 份提案
    existing_nums = {p.proposal_number for p in
                     SelfStudyProposal.query.filter_by(
                         user_id=current_user.id, semester=SEMESTER).all()}
    for n in CONTROL_GROUP_TASKS:
        if n not in existing_nums:
            db.session.add(SelfStudyProposal(
                user_id=current_user.id,
                proposal_number=n,
                semester=SEMESTER,
            ))
    if len(existing_nums) < 4:
        db.session.commit()

    proposals = SelfStudyProposal.query.filter_by(
        user_id=current_user.id, semester=SEMESTER
    ).order_by(SelfStudyProposal.proposal_number).all()
    return render_template('student/self_study_list.html',
                           proposals=proposals,
                           ctrl_tasks=CONTROL_GROUP_TASKS)


@app.route('/self-study/new', methods=['POST'])
@login_required
def self_study_new():
    # 4 份提案由系統自動建立，此路由保留以兼容舊書籤
    flash('四份自主學習提案已由系統自動建立，請直接點選進入填寫。', 'info')
    return redirect(url_for('self_study_list'))


@app.route('/self-study/<int:n>', methods=['GET', 'POST'])
@login_required
def self_study_detail(n):
    if current_user.is_teacher:
        return redirect(url_for('teacher_self_study_list'))
    if current_user.experimental_group != 'control':
        flash('此功能僅開放給對照組學生。', 'error')
        return redirect(url_for('dashboard'))
    if n not in range(1, 5):
        flash('無效的提案編號。', 'error')
        return redirect(url_for('self_study_list'))

    proposal = SelfStudyProposal.query.filter_by(
        user_id=current_user.id, proposal_number=n, semester=SEMESTER
    ).first_or_404()

    if request.method == 'POST':
        action = request.form.get('action')

        # ── 儲存/提交提案 ──
        if action in ('save_draft', 'submit_proposal') and \
                proposal.approval_status in ('draft', 'revise_needed'):
            proposal.topic           = request.form.get('subtitle', '').strip()  # 副標題
            proposal.motivation      = request.form.get('motivation', '').strip()
            proposal.expected_output = request.form.get('expected_output', '').strip()
            proposal.schedule        = request.form.get('schedule', '').strip()
            if action == 'submit_proposal':
                if not all([proposal.motivation, proposal.expected_output]):
                    flash('請填寫學習計畫與預期成果後再提交。', 'error')
                    t_def = CONTROL_GROUP_TASKS.get(n, {})
                    return render_template('student/self_study_detail.html',
                                           proposal=proposal, task_def=t_def)
                proposal.approval_status = 'submitted'
                proposal.proposed_at = datetime.utcnow()
                flash(f'提案 {n} 已送出，等待教師審核。', 'success')
            else:
                flash('草稿已儲存。', 'info')
            db.session.commit()
            return redirect(url_for('self_study_detail', n=n))

        # ── 提交成果 ──
        if action == 'submit_result' and proposal.approval_status == 'approved':
            proposal.result_content = request.form.get('result_content', '').strip()
            proposal.reflection     = request.form.get('reflection', '').strip()

            file = request.files.get('result_file')
            if file and file.filename and allowed_file(file.filename):
                upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], str(current_user.id))
                os.makedirs(upload_dir, exist_ok=True)
                safe_name = secure_filename(
                    f"{current_user.student_id}_ss{n}_{file.filename}"
                )
                fpath = os.path.join(upload_dir, safe_name)
                file.save(fpath)
                proposal.result_file_path = fpath
                proposal.result_file_name = file.filename

            if not proposal.result_content:
                flash('請填寫成果說明後再提交。', 'error')
                t_def = CONTROL_GROUP_TASKS.get(n, {})
                return render_template('student/self_study_detail.html',
                                       proposal=proposal, task_def=t_def)
            proposal.approval_status    = 'result_submitted'
            proposal.result_submitted_at = datetime.utcnow()
            db.session.commit()
            flash(f'提案 {n} 成果已送出，等待教師評閱。', 'success')
            return redirect(url_for('self_study_detail', n=n))

    t_def = CONTROL_GROUP_TASKS.get(n, {})
    return render_template('student/self_study_detail.html',
                           proposal=proposal, task_def=t_def)


# ─── Student: Learning Journal ────────────────────────────────────────────────

def _journal_for_group(j_def, group):
    """Return journal def with group-appropriate title and prompt."""
    if group == 'control' and 'title_control' in j_def:
        return {**j_def,
                'title':  j_def['title_control'],
                'prompt': j_def['prompt_control']}
    return j_def


@app.route('/journal')
@login_required
def journal_list():
    group = current_user.experimental_group
    journals = {j['journal_number']: _journal_for_group(j, group)
                for j in LEARNING_JOURNALS}
    submitted = {
        lj.journal_number: lj
        for lj in LearningJournal.query.filter_by(
            user_id=current_user.id, semester=SEMESTER).all()
    }
    return render_template('student/journal_list.html',
                           journals=journals,
                           submitted=submitted)


@app.route('/journal/<int:journal_number>', methods=['GET', 'POST'])
@login_required
def view_journal(journal_number):
    j_defs = {j['journal_number']: _journal_for_group(j, current_user.experimental_group)
              for j in LEARNING_JOURNALS}
    j_def = j_defs.get(journal_number)
    if not j_def:
        flash('無效的日誌編號。', 'error')
        return redirect(url_for('journal_list'))

    existing = LearningJournal.query.filter_by(
        user_id=current_user.id,
        journal_number=journal_number,
        semester=SEMESTER
    ).first()

    if request.method == 'POST':
        content = request.form.get('content', '').strip()
        if not content:
            flash('請填寫日誌內容。', 'error')
        else:
            eval_json = existing.evaluation_json if existing else ''
            if journal_number == 5:
                dp5_rating   = request.form.get('dp5_self_rating', '').strip()
                dp5_evidence = request.form.get('dp5_evidence', '').strip()
                try:
                    ev = json.loads(eval_json) if eval_json else {}
                except Exception:
                    ev = {}
                rating_val = None
                if dp5_rating:
                    try:
                        v = int(dp5_rating)
                        if 1 <= v <= 5:
                            rating_val = v
                    except ValueError:
                        pass
                ev['DP5'] = {'self_rating': rating_val, 'evidence': dp5_evidence}
                eval_json = json.dumps(ev, ensure_ascii=False)

            if existing:
                existing.content    = content
                existing.updated_at = datetime.utcnow()
                if journal_number == 5:
                    existing.evaluation_json = eval_json
            else:
                existing = LearningJournal(
                    user_id          = current_user.id,
                    journal_number   = journal_number,
                    week             = j_def['week'],
                    semester         = SEMESTER,
                    content          = content,
                    evaluation_json  = eval_json if journal_number == 5 else '',
                )
                db.session.add(existing)
            db.session.commit()
            flash('學習日誌已儲存。', 'success')
            return redirect(url_for('view_journal', journal_number=journal_number))

    dp5_data = {}
    if journal_number == 5 and existing and existing.evaluation_json:
        try:
            dp5_data = json.loads(existing.evaluation_json).get('DP5', {})
        except Exception:
            pass

    return render_template('student/journal.html',
                           j_def=j_def,
                           existing=existing,
                           dp5_data=dp5_data)


# ─── Student: Questionnaire ───────────────────────────────────────────────────

@app.route('/questionnaire/<string:q_code>', methods=['GET', 'POST'])
@login_required
def view_questionnaire(q_code):
    q = Questionnaire.query.filter_by(code=q_code).first_or_404()
    if not q.is_active:
        flash('此問卷目前尚未開放。', 'error')
        return redirect(url_for('dashboard'))

    existing = QuestionnaireSubmission.query.filter_by(
        user_id=current_user.id,
        questionnaire_id=q.id
    ).first()

    if request.method == 'POST':
        if existing:
            flash('你已填寫過此問卷。', 'warning')
            return redirect(url_for('dashboard'))

        qs = QuestionnaireSubmission(
            user_id          = current_user.id,
            questionnaire_id = q.id,
        )
        db.session.add(qs)
        db.session.flush()

        for item in q.items:
            value = request.form.get(f'q_{item.item_code}', '').strip()
            db.session.add(QuestionnaireAnswer(
                submission_id = qs.id,
                item_code     = item.item_code,   # e.g. "arcsa_a1"
                value         = value,
            ))
        db.session.commit()
        flash('問卷提交成功，感謝你的填寫！', 'success')
        return redirect(url_for('dashboard'))

    # 整理已有答案（若允許再次檢視）
    existing_answers = {}
    if existing:
        existing_answers = {
            a.item_code: a.value
            for a in existing.answers
        }

    return render_template('student/questionnaire.html',
                           q=q,
                           existing=existing,
                           existing_answers=existing_answers)


# ─── Student: Competency Radar ────────────────────────────────────────────────

@app.route('/my/competency')
@login_required
def my_competency():
    if current_user.is_teacher:
        return redirect(url_for('dashboard'))
    from services.competency import aggregate_competency, compute_arcsa_ac_perception
    competency = aggregate_competency(current_user.id, SEMESTER)
    arcsa      = compute_arcsa_ac_perception(current_user.id)
    return render_template('student/competency.html',
                           competency=competency,
                           arcsa=arcsa,
                           axes_desc=AXES_DESCRIPTIONS)


# ─── v1 Legacy: Submission Detail (read-only) ─────────────────────────────────

@app.route('/submission/<int:submission_id>')
@login_required
def view_submission(submission_id):
    submission = db.session.get(Submission, submission_id)
    if not submission:
        flash('找不到此提交。', 'error')
        return redirect(url_for('dashboard'))
    if not current_user.is_teacher and submission.user_id != current_user.id:
        flash('無權限查看。', 'error')
        return redirect(url_for('dashboard'))

    ai_fb = submission.ai_feedbacks.first()
    teacher_review = submission.teacher_reviews.first()
    if not current_user.is_teacher and teacher_review and not teacher_review.published:
        teacher_review = None

    return render_template('student/submission_detail.html',
                           submission=submission,
                           ai_feedback=ai_fb,
                           teacher_review=teacher_review)


# ─── Teacher Routes ───────────────────────────────────────────────────────────

@app.route('/teacher')
@login_required
def teacher_dashboard():
    if not current_user.is_teacher:
        flash('無教師權限。', 'error')
        return redirect(url_for('dashboard'))

    pending_users = User.query.filter_by(role='student', status='pending')\
        .order_by(User.created_at.asc()).all()
    students = User.query.filter_by(role='student', status='active')\
        .order_by(User.class_group, User.student_id).all()
    disabled_users = User.query.filter_by(role='student', status='disabled')\
        .order_by(User.class_group, User.student_id).all()
    reset_requests = User.query.filter(
        User.role == 'student',
        User.reset_requested_at != None
    ).order_by(User.reset_requested_at.asc()).all()
    # 只計實驗組的任務提交
    experimental_user_ids = db.session.query(User.id).filter(
        User.role == 'student',
        User.status == 'active',
        User.experimental_group == 'experimental'
    ).subquery()
    experimental_count = db.session.query(db.func.count()).select_from(
        User
    ).filter(
        User.role == 'student',
        User.status == 'active',
        User.experimental_group == 'experimental'
    ).scalar()
    total_subs = TaskSubmission.query.filter(
        TaskSubmission.semester == SEMESTER,
        TaskSubmission.status != 'draft',
        TaskSubmission.user_id.in_(experimental_user_ids)
    ).count()
    reviewed_count = TeacherReview.query.filter_by(published=True).count()

    task_stats = {}
    for t_num, t_def in TASKS.items():
        subs = TaskSubmission.query.filter(
            TaskSubmission.task_number == t_num,
            TaskSubmission.semester == SEMESTER,
            TaskSubmission.status != 'draft',
            TaskSubmission.user_id.in_(experimental_user_ids)
        ).all()
        task_stats[t_num] = {
            'name':              t_def['name'],
            'total_submissions': len(subs),
            'unique_students':   len(set(s.user_id for s in subs)),
            'reviewed':          sum(
                1 for s in subs
                if s.teacher_reviews.filter_by(published=True).first()
            ),
        }

    # 各學生提案數（對照組用）
    proposal_counts = {
        uid: count
        for uid, count in db.session.query(
            SelfStudyProposal.user_id,
            db.func.count(SelfStudyProposal.id)
        ).filter(
            SelfStudyProposal.semester == SEMESTER,
            SelfStudyProposal.approval_status != 'draft'
        ).group_by(SelfStudyProposal.user_id).all()
    }

    return render_template('teacher/dashboard.html',
                           students=students,
                           pending_users=pending_users,
                           disabled_users=disabled_users,
                           reset_requests=reset_requests,
                           total_submissions=total_subs,
                           reviewed_count=reviewed_count,
                           task_stats=task_stats,
                           tasks=TASKS,
                           proposal_counts=proposal_counts,
                           semester=SEMESTER,
                           experimental_count=experimental_count)


@app.route('/teacher/user/<int:uid>/approve', methods=['POST'])
@login_required
@csrf_required
def teacher_approve_user(uid):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    user = db.session.get(User, uid)
    if user and user.status == 'pending':
        user.status = 'active'
        db.session.commit()
        flash(f'已核准 {user.name}（{user.student_id}）的帳號。', 'success')
    return redirect(url_for('teacher_dashboard'))


@app.route('/teacher/user/<int:uid>/reject', methods=['POST'])
@login_required
@csrf_required
def teacher_reject_user(uid):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    user = db.session.get(User, uid)
    if user and user.status == 'pending':
        name = user.name
        sid  = user.student_id
        db.session.delete(user)
        db.session.commit()
        flash(f'已拒絕並刪除 {name}（{sid}）的帳號申請。', 'success')
    return redirect(url_for('teacher_dashboard'))


@app.route('/teacher/user/<int:uid>/toggle', methods=['POST'])
@login_required
@csrf_required
def teacher_toggle_user(uid):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    user = db.session.get(User, uid)
    if user and user.role == 'student':
        user.status = 'disabled' if user.status == 'active' else 'active'
        db.session.commit()
        state = '停用' if user.status == 'disabled' else '啟用'
        flash(f'已{state} {user.name}（{user.student_id}）的帳號。', 'success')
    return redirect(url_for('teacher_dashboard'))


@app.route('/teacher/user/<int:uid>/delete', methods=['POST'])
@login_required
@csrf_required
def teacher_delete_user(uid):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    user = db.session.get(User, uid)
    if user and user.role == 'student':
        name = user.name
        sid  = user.student_id

        # 用 raw SQL 按外鍵依賴順序刪除，避免所有 NOT NULL 衝突
        from sqlalchemy import text
        with db.engine.connect() as conn:
            # 問卷答案 → 問卷提交
            conn.execute(text("""
                DELETE FROM questionnaire_answers
                WHERE submission_id IN (
                    SELECT id FROM questionnaire_submissions WHERE user_id = :uid
                )
            """), {'uid': uid})
            conn.execute(text(
                "DELETE FROM questionnaire_submissions WHERE user_id = :uid"
            ), {'uid': uid})

            # 學習日誌
            conn.execute(text(
                "DELETE FROM learning_journals WHERE user_id = :uid"
            ), {'uid': uid})

            # v2.5.0 新增表
            conn.execute(text(
                "DELETE FROM learning_events WHERE user_id = :uid"
            ), {'uid': uid})
            conn.execute(text(
                "DELETE FROM self_study_proposals WHERE user_id = :uid"
            ), {'uid': uid})
            conn.execute(text(
                "DELETE FROM oral_presentation_assessments WHERE user_id = :uid"
            ), {'uid': uid})

            # 訊息與已讀記錄（含此使用者寄出 / 收到的所有訊息）
            conn.execute(text("""
                DELETE FROM message_reads
                WHERE user_id = :uid
                   OR message_id IN (
                       SELECT id FROM messages
                       WHERE sender_id = :uid OR recipient_id = :uid
                   )
            """), {'uid': uid})
            conn.execute(text("""
                DELETE FROM messages
                WHERE sender_id = :uid OR recipient_id = :uid
            """), {'uid': uid})

            # AI tutor 對話、工作坊參與
            conn.execute(text(
                "DELETE FROM tutor_conversations WHERE user_id = :uid"
            ), {'uid': uid})
            conn.execute(text(
                "DELETE FROM workshop_participations WHERE user_id = :uid"
            ), {'uid': uid})

            # snapshots 參照 ai_feedbacks，須在刪 ai_feedbacks 前處理
            conn.execute(text("""
                DELETE FROM task_submission_snapshots
                WHERE task_submission_id IN (
                    SELECT id FROM task_submissions WHERE user_id = :uid
                )
            """), {'uid': uid})

            # AI review suggestions（FK 到 task_submissions，須在刪 task_submissions 前）
            conn.execute(text("""
                DELETE FROM ai_review_suggestions
                WHERE task_submission_id IN (
                    SELECT id FROM task_submissions WHERE user_id = :uid
                )
            """), {'uid': uid})

            # 任務日期 audit：set_by / changed_by 是 nullable FK，set NULL 即可
            conn.execute(text(
                "UPDATE task_schedules SET set_by = NULL WHERE set_by = :uid"
            ), {'uid': uid})
            conn.execute(text(
                "UPDATE task_date_change_logs SET changed_by = NULL WHERE changed_by = :uid"
            ), {'uid': uid})

            # v2 任務提交的所有子表
            conn.execute(text("""
                DELETE FROM ai_feedbacks
                WHERE task_submission_id IN (
                    SELECT id FROM task_submissions WHERE user_id = :uid
                )
            """), {'uid': uid})
            conn.execute(text("""
                DELETE FROM teacher_reviews
                WHERE task_submission_id IN (
                    SELECT id FROM task_submissions WHERE user_id = :uid
                )
            """), {'uid': uid})
            conn.execute(text("""
                DELETE FROM question_responses
                WHERE submission_id IN (
                    SELECT id FROM task_submissions WHERE user_id = :uid
                )
            """), {'uid': uid})
            conn.execute(text("""
                DELETE FROM checklist_responses
                WHERE submission_id IN (
                    SELECT id FROM task_submissions WHERE user_id = :uid
                )
            """), {'uid': uid})
            conn.execute(text("""
                DELETE FROM reflection_responses
                WHERE submission_id IN (
                    SELECT id FROM task_submissions WHERE user_id = :uid
                )
            """), {'uid': uid})
            conn.execute(text("""
                DELETE FROM deliverable_uploads
                WHERE submission_id IN (
                    SELECT id FROM task_submissions WHERE user_id = :uid
                )
            """), {'uid': uid})
            conn.execute(text(
                "DELETE FROM task_submissions WHERE user_id = :uid"
            ), {'uid': uid})

            # v1 舊提交的子表
            conn.execute(text("""
                DELETE FROM ai_feedbacks
                WHERE submission_id IN (
                    SELECT id FROM submissions WHERE user_id = :uid
                )
            """), {'uid': uid})
            conn.execute(text("""
                DELETE FROM teacher_reviews
                WHERE submission_id IN (
                    SELECT id FROM submissions WHERE user_id = :uid
                )
            """), {'uid': uid})
            conn.execute(text(
                "DELETE FROM submissions WHERE user_id = :uid"
            ), {'uid': uid})

            # 最後刪除使用者
            conn.execute(text("DELETE FROM users WHERE id = :uid"), {'uid': uid})
            conn.commit()

        flash(f'已刪除 {name}（{sid}）的帳號及所有資料。', 'success')
    return redirect(url_for('teacher_dashboard'))


@app.route('/teacher/user/<int:uid>/reset-password', methods=['POST'])
@login_required
@csrf_required
def teacher_reset_password(uid):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    user = db.session.get(User, uid)
    if not user or user.role != 'student':
        flash('找不到此學生帳號。', 'error')
        return redirect(url_for('teacher_dashboard'))
    new_password = request.form.get('new_password', '').strip()
    if len(new_password) < 6:
        flash('新密碼至少需要 6 個字元。', 'error')
        return redirect(url_for('teacher_dashboard'))
    user.set_password(new_password)
    user.reset_requested_at  = None
    user.reset_contact_email = None
    msg = Message(
        sender_id=current_user.id,
        recipient_id=user.id,
        scope='personal',
        subject='密碼已由教師重設',
        body=f'您的帳號密碼已由教師重設。\n\n臨時密碼：{new_password}\n\n請登入後盡快至「修改密碼」頁面更改為您自己的密碼。'
    )
    db.session.add(msg)
    db.session.commit()
    flash(f'已重設 {user.name} 的密碼，並已傳送私訊通知。', 'success')
    return redirect(url_for('teacher_dashboard'))


@app.route('/teacher/manage-groups', methods=['GET', 'POST'])
@login_required
def teacher_manage_groups():
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))

    students = User.query.filter_by(role='student', status='active')\
        .order_by(User.class_group, User.student_id).all()

    if request.method == 'POST':
        updated = 0
        for student in students:
            val = request.form.get(f'group_{student.id}', '')
            new_group = val if val in ('experimental', 'control') else None
            old_group = student.experimental_group
            if old_group != new_group:
                student.experimental_group = new_group
                updated += 1
                # 新分配至對照組 → 自動建立 4 份固定提案
                if new_group == 'control':
                    existing_nums = {p.proposal_number for p in
                                     SelfStudyProposal.query.filter_by(
                                         user_id=student.id, semester=SEMESTER).all()}
                    for n in CONTROL_GROUP_TASKS:
                        if n not in existing_nums:
                            db.session.add(SelfStudyProposal(
                                user_id=student.id,
                                proposal_number=n,
                                semester=SEMESTER,
                            ))
        db.session.commit()
        flash(f'已更新 {updated} 位學生的研究分組。', 'success')
        return redirect(url_for('teacher_manage_groups'))

    group_counts = {
        'experimental': sum(1 for s in students if s.experimental_group == 'experimental'),
        'control':      sum(1 for s in students if s.experimental_group == 'control'),
        'unassigned':   sum(1 for s in students if not s.experimental_group),
    }
    return render_template('teacher/manage_groups.html',
                           students=students,
                           group_counts=group_counts)


@app.route('/teacher/self-study')
@login_required
def teacher_self_study_list():
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))

    proposals = (SelfStudyProposal.query
                 .join(User, User.id == SelfStudyProposal.user_id)
                 .filter(User.experimental_group == 'control',
                         SelfStudyProposal.semester == SEMESTER)
                 .order_by(User.student_id, SelfStudyProposal.proposal_number)
                 .all())

    tab_groups = {
        'submitted':        [p for p in proposals if p.approval_status == 'submitted'],
        'approved':         [p for p in proposals if p.approval_status == 'approved'],
        'result_submitted': [p for p in proposals if p.approval_status == 'result_submitted'],
        'finalized':        [p for p in proposals if p.approval_status == 'finalized'],
        'other':            [p for p in proposals if p.approval_status in ('draft', 'revise_needed', 'overdue')],
    }
    return render_template('teacher/self_study_list.html',
                           tab_groups=tab_groups)


@app.route('/teacher/self-study/<int:proposal_id>', methods=['GET'])
@login_required
def teacher_self_study_review(proposal_id):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    proposal = db.session.get(SelfStudyProposal, proposal_id)
    if not proposal:
        flash('找不到此提案。', 'error')
        return redirect(url_for('teacher_self_study_list'))
    student  = db.session.get(User, proposal.user_id)
    task_def = CONTROL_GROUP_TASKS.get(proposal.proposal_number, {})
    score_axes = task_def.get('axes', ['DP1', 'DP2', 'DP3', 'DP4'])
    return render_template('teacher/self_study_review.html',
                           proposal=proposal,
                           student=student,
                           task_def=task_def,
                           score_axes=score_axes,
                           axes_desc=AXES_DESCRIPTIONS)


@app.route('/teacher/self-study/<int:proposal_id>/review', methods=['POST'])
@login_required
@csrf_required
def teacher_self_study_approve(proposal_id):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    proposal = db.session.get(SelfStudyProposal, proposal_id)
    if not proposal or proposal.approval_status != 'submitted':
        flash('此提案目前不可審核。', 'error')
        return redirect(url_for('teacher_self_study_list'))

    decision = request.form.get('decision')

    # v5 規範：核准前必須勾選每個評量向度的最低證據
    if decision == 'approve':
        axes = _proposal_axes(proposal.proposal_number)
        missing = [ax for ax in axes if request.form.get(f'evidence_{ax}') != 'on']
        if missing:
            flash(f'請先勾選所有評量向度的證據確認（缺：{", ".join(missing)}）。', 'error')
            return redirect(url_for('teacher_self_study_review', proposal_id=proposal.id))

    proposal.teacher_comment = request.form.get('teacher_comment', '').strip()
    proposal.reviewed_by     = current_user.id
    proposal.reviewed_at     = datetime.utcnow()

    topic_label = f'「{proposal.topic}」' if proposal.topic else f'提案 {proposal.proposal_number}'
    if decision == 'approve':
        proposal.approval_status = 'approved'
        msg_body = (
            f'您的自主學習{topic_label}已獲教師核准。\n\n'
            f'請依計畫完成自主學習，並回到提案頁面繳交成果。'
        )
        if proposal.teacher_comment:
            msg_body += f'\n\n教師備注：{proposal.teacher_comment}'
        flash('提案已核准。', 'success')
    else:
        proposal.approval_status = 'revise_needed'
        msg_body = (
            f'您的自主學習{topic_label}需要修改後再提交。\n\n'
            f'請回到提案頁面依據教師意見修改並重新提交。'
        )
        if proposal.teacher_comment:
            msg_body += f'\n\n教師意見：{proposal.teacher_comment}'
        flash('已退回學生修改提案。', 'info')

    db.session.add(Message(
        sender_id=current_user.id,
        recipient_id=proposal.user_id,
        scope='personal',
        subject=f'自主學習提案審核結果：{topic_label}',
        body=msg_body
    ))
    db.session.commit()
    return redirect(url_for('teacher_self_study_list'))


@app.route('/teacher/self-study/<int:proposal_id>/finalize', methods=['POST'])
@login_required
@csrf_required
def teacher_self_study_finalize(proposal_id):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    proposal = db.session.get(SelfStudyProposal, proposal_id)
    if not proposal or proposal.approval_status != 'result_submitted':
        flash('此提案目前不可評閱。', 'error')
        return redirect(url_for('teacher_self_study_list'))

    rubric_scores = {}
    for ax in _proposal_axes(proposal.proposal_number):
        val = request.form.get(f'score_{ax}', '').strip()
        if val:
            try:
                score = int(val)
                if 1 <= score <= 5:
                    rubric_scores[ax] = score
            except ValueError:
                pass

    import json as _json
    proposal.rubric_json     = _json.dumps(rubric_scores, ensure_ascii=False)
    proposal.final_feedback  = request.form.get('final_feedback', '').strip()
    proposal.reviewer_id     = current_user.id
    proposal.finalized_at    = datetime.utcnow()
    proposal.approval_status = 'finalized'
    db.session.commit()
    flash('成果已評閱並完成認證。', 'success')
    return redirect(url_for('teacher_self_study_list'))


@app.route('/teacher/self-study/<int:proposal_id>/ai_rubric_suggestion')
@login_required
def teacher_self_study_ai_rubric(proposal_id):
    if not current_user.is_teacher:
        return jsonify({'error': 'forbidden'}), 403
    proposal = db.session.get(SelfStudyProposal, proposal_id)
    if not proposal:
        return jsonify({'error': 'not_found'}), 404
    if not app.config.get('ANTHROPIC_API_KEY'):
        return jsonify({'error': 'ai_disabled',
                        'message': 'AI 建議功能尚未啟用（缺少 ANTHROPIC_API_KEY）。'}), 200

    axes     = _proposal_axes(proposal.proposal_number)
    text_parts = []
    if proposal.topic:
        text_parts.append(f'【學習主題】{proposal.topic}')
    if proposal.motivation:
        text_parts.append(f'【學習動機與目標】{proposal.motivation}')
    if proposal.expected_output:
        text_parts.append(f'【預期成果】{proposal.expected_output}')
    if proposal.result_content:
        text_parts.append(f'【成果報告】{proposal.result_content}')
    if proposal.reflection:
        text_parts.append(f'【學習反思】{proposal.reflection}')
    proposal_text = '\n\n'.join(text_parts)

    if not proposal_text.strip():
        return jsonify({'error': 'empty', 'message': '提案內容為空，無法產生建議。'}), 200

    from ai_service import generate_self_study_rubric_suggestion
    result = generate_self_study_rubric_suggestion(proposal_text, axes, AXES_DESCRIPTIONS)
    return jsonify(result)


@app.route('/profile/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_pw  = request.form.get('current_password', '')
        new_pw      = request.form.get('new_password', '').strip()
        confirm_pw  = request.form.get('confirm_password', '').strip()
        if not current_user.check_password(current_pw):
            flash('目前密碼不正確。', 'error')
            return render_template('student/change_password.html')
        if len(new_pw) < 6:
            flash('新密碼至少需要 6 個字元。', 'error')
            return render_template('student/change_password.html')
        if new_pw != confirm_pw:
            flash('兩次輸入的新密碼不一致。', 'error')
            return render_template('student/change_password.html')
        current_user.set_password(new_pw)
        db.session.commit()
        flash('密碼已更新。', 'success')
        return redirect(url_for('dashboard'))
    return render_template('student/change_password.html')


@app.route('/teacher/task/<int:task_number>')
@login_required
def teacher_task_submissions(task_number):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    task_def = TASKS.get(task_number, {})
    subs = TaskSubmission.query.filter(
        TaskSubmission.task_number == task_number,
        TaskSubmission.semester == SEMESTER,
        TaskSubmission.status != 'draft'
    ).order_by(TaskSubmission.submitted_at.desc()).all()
    return render_template('teacher/submissions.html',
                           task_number=task_number,
                           task_def=task_def,
                           submissions=subs)


@app.route('/teacher/review/<int:submission_id>', methods=['GET', 'POST'])
@login_required
def teacher_review(submission_id):
    if request.method == 'POST':
        sent = request.form.get('_csrf') or request.headers.get('X-CSRFToken', '')
        expected = session.get('_csrf_token', '')
        if not expected or not secrets.compare_digest(sent, expected):
            abort(400, 'CSRF token invalid or missing.')
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))

    sub = db.session.get(TaskSubmission, submission_id)
    if not sub:
        flash('找不到此提交。', 'error')
        return redirect(url_for('teacher_dashboard'))

    task_def = TASKS.get(sub.task_number, {})

    existing_review = sub.teacher_reviews.order_by(TeacherReview.id.asc()).first()

    if request.method == 'POST':
        action = request.form.get('action', 'feedback')

        if action in ('rubric_save', 'rubric_finalize'):
            # 已 finalized 的 Rubric 不可再覆寫（學生雷達圖讀的是 finalized 後的數值）
            if existing_review and existing_review.rubric_finalized_at:
                flash('此 Rubric 已鎖定，不可再修改。', 'error')
                return redirect(url_for('teacher_review', submission_id=sub.id))

            rubric_axes = task_def.get('axes', [])
            rubric_scores = {}
            for ax in rubric_axes:
                val = request.form.get(f'rubric_{ax}', '').strip()
                if not val:
                    continue
                try:
                    s = int(val)
                except ValueError:
                    continue
                if 1 <= s <= 5:
                    rubric_scores[ax] = s

            if not existing_review:
                existing_review = TeacherReview(
                    task_submission_id=sub.id,
                    teacher_id=current_user.id,
                    feedback='',
                )
                db.session.add(existing_review)
                db.session.flush()

            existing_review.rubric_json   = json.dumps(rubric_scores, ensure_ascii=False)
            existing_review.rubric_source = request.form.get('rubric_source_hint', 'teacher_manual')

            if action == 'rubric_finalize':
                if len(rubric_scores) == len(rubric_axes) and rubric_axes:
                    existing_review.rubric_finalized_at = datetime.utcnow()
                    if existing_review.rubric_source == 'ai_adopted':
                        existing_review.rubric_source = 'ai_adopted_then_confirmed'
                    db.session.commit()
                    flash('Rubric 已確認鎖定。', 'success')
                else:
                    db.session.commit()
                    flash('請填寫全部評量向度後再確認。', 'error')
            else:
                db.session.commit()
                flash('Rubric 已暫存。', 'success')
            return redirect(url_for('teacher_review', submission_id=sub.id))

        # action == 'feedback'（預設）
        feedback = request.form.get('feedback', '').strip()
        score_raw = request.form.get('score', '').strip()
        publish  = request.form.get('publish') == 'on'

        score_val = None
        if score_raw:
            try:
                v = float(score_raw)
                if 0 <= v <= 100:
                    score_val = v
            except ValueError:
                flash('分數格式錯誤（需 0–100）。', 'error')
                return redirect(url_for('teacher_review', submission_id=sub.id))

        if existing_review:
            existing_review.feedback    = feedback
            existing_review.score       = score_val
            existing_review.published   = publish
            existing_review.reviewed_at = datetime.utcnow()
        else:
            db.session.add(TeacherReview(
                task_submission_id = sub.id,
                teacher_id         = current_user.id,
                feedback           = feedback,
                score              = score_val,
                published          = publish,
            ))
        if publish:
            sub.status = 'reviewed'
        db.session.commit()
        flash('評閱已儲存。' + (' 已發布給學生。' if publish else ''), 'success')
        return redirect(url_for('teacher_review', submission_id=sub.id))

    # 整理回答供顯示
    pq_map = {r.question_id: r.answer for r in sub.question_responses}
    cl_map = {r.item_id: r            for r in sub.checklist_responses}
    rq_map = {r.question_id: r.answer for r in sub.reflection_responses}
    du_map = {du.deliverable_id: du   for du in sub.deliverable_uploads}

    ai_fb = sub.ai_feedbacks.order_by(AIFeedback.created_at.desc()).first()

    rubric_axes = task_def.get('axes', [])
    rubric_data = {}
    if existing_review and existing_review.rubric_json:
        try:
            rubric_data = json.loads(existing_review.rubric_json)
        except Exception:
            pass

    # AI 建議改由前端 AJAX 呼叫 /teacher/review/<id>/ai_suggestion 取得，
    # 避免每次打開頁面都阻塞等 Claude 回應。
    return render_template('teacher/review.html',
                           sub=sub,
                           task_def=task_def,
                           pq_map=pq_map,
                           cl_map=cl_map,
                           rq_map=rq_map,
                           du_map=du_map,
                           ai_feedback=ai_fb,
                           existing_review=existing_review,
                           rubric_axes=rubric_axes,
                           rubric_data=rubric_data,
                           axes_desc=AXES_DESCRIPTIONS)


@app.route('/teacher/review/<int:submission_id>/ai_rubric_scores')
@login_required
def teacher_review_ai_rubric_scores(submission_id):
    if not current_user.is_teacher:
        return jsonify({'error': 'forbidden'}), 403
    sub = db.session.get(TaskSubmission, submission_id)
    if not sub:
        return jsonify({'error': 'not_found'}), 404
    if not app.config.get('ANTHROPIC_API_KEY'):
        return jsonify({'error': 'ai_disabled',
                        'message': 'AI 建議功能尚未啟用（缺少 ANTHROPIC_API_KEY）。'}), 200
    task_def = TASKS.get(sub.task_number, {})
    axes = task_def.get('axes', [])
    if not axes:
        return jsonify({'error': 'no_axes', 'message': '此任務無 Rubric 向度設定。'}), 200
    text = _build_submission_text_for_ai(sub, task_def)
    if not text.strip():
        return jsonify({'error': 'empty', 'message': '提交內容為空。'}), 200
    from ai_service import generate_self_study_rubric_suggestion
    result = generate_self_study_rubric_suggestion(text, axes, AXES_DESCRIPTIONS)
    return jsonify(result)


@app.route('/teacher/review/<int:submission_id>/ai_suggestion')
@login_required
def teacher_review_ai_suggestion(submission_id):
    """回傳該 submission 的 AI 評閱建議。
    - 預設讀快取；若 submission 自上次產生後被更新，或 ?force=1，則重新向 Claude 索取並覆寫快取。
    - 回傳 JSON：{ suggestion, suggested_score, rubric_notes, cached, generated_at }
    """
    if not current_user.is_teacher:
        return jsonify({'error': 'forbidden'}), 403

    sub = db.session.get(TaskSubmission, submission_id)
    if not sub:
        return jsonify({'error': 'not_found'}), 404

    if not app.config.get('ANTHROPIC_API_KEY'):
        return jsonify({'error': 'ai_disabled',
                        'message': 'AI 建議功能尚未啟用（缺少 ANTHROPIC_API_KEY）。'}), 200

    force = request.args.get('force') == '1'
    cached = AIReviewSuggestion.query.filter_by(task_submission_id=sub.id).first()

    # 命中快取：submission 沒動過且非強制重算
    if cached and not force and cached.source_updated_at >= sub.updated_at:
        return jsonify({
            'suggestion':      cached.suggestion,
            'suggested_score': cached.suggested_score,
            'rubric_notes':    cached.rubric_notes,
            'cached':          True,
            'generated_at':    cached.created_at.isoformat(),
        })

    # 快取失效或強制重算 → 呼叫 Claude
    task_def = TASKS.get(sub.task_number, {})
    submission_text = _build_submission_text_for_ai(sub, task_def)
    if not submission_text.strip():
        return jsonify({'error': 'empty_submission',
                        'message': '學生提交內容為空，無法產生建議。'}), 200

    result = ai_service.generate_review_suggestion(
        submission_text, sub.task_number, 'structured'
    )
    if not isinstance(result, dict):
        result = {'suggestion': str(result)}

    suggestion_text = result.get('suggestion') or ''
    # 若 ai_service 回傳錯誤佔位字串，不寫入快取（避免把錯誤當成結果長期保留）
    is_error_payload = suggestion_text.startswith('AI 建議生成失敗')

    if not is_error_payload:
        if cached:
            cached.raw_json          = json.dumps(result, ensure_ascii=False)
            cached.suggestion        = suggestion_text
            cached.suggested_score   = result.get('suggested_score')
            cached.rubric_notes      = result.get('rubric_notes') or ''
            cached.source_updated_at = sub.updated_at
            cached.model_used        = 'claude-sonnet-4-5'
            cached.created_at        = datetime.utcnow()
        else:
            cached = AIReviewSuggestion(
                task_submission_id = sub.id,
                raw_json           = json.dumps(result, ensure_ascii=False),
                suggestion         = suggestion_text,
                suggested_score    = result.get('suggested_score'),
                rubric_notes       = result.get('rubric_notes') or '',
                source_updated_at  = sub.updated_at,
                model_used         = 'claude-sonnet-4-5',
            )
            db.session.add(cached)
        db.session.commit()

    return jsonify({
        'suggestion':      suggestion_text,
        'suggested_score': result.get('suggested_score'),
        'rubric_notes':    result.get('rubric_notes') or '',
        'cached':          False,
        'generated_at':    datetime.utcnow().isoformat(),
        'error':           'ai_call_failed' if is_error_payload else None,
    })


@app.route('/teacher/student/<int:uid>')
@login_required
def teacher_student_profile(uid):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))

    student = db.session.get(User, uid)
    if not student or student.role != 'student':
        flash('找不到此學生。', 'error')
        return redirect(url_for('teacher_dashboard'))

    # 各任務提交（含最新 AI 回饋與教師評分）
    task_submissions = {}
    for t_num in TASKS:
        sub = TaskSubmission.query.filter_by(
            user_id=student.id, task_number=t_num, semester=SEMESTER
        ).order_by(TaskSubmission.submitted_at.desc()).first()
        task_submissions[t_num] = sub

    # 問卷填答狀態
    questionnaires = Questionnaire.query.filter_by(semester=SEMESTER).all()
    questionnaire_status = {}
    for q in questionnaires:
        submission = QuestionnaireSubmission.query.filter_by(
            user_id=student.id, questionnaire_id=q.id
        ).first()
        questionnaire_status[q.code] = {
            'questionnaire': q,
            'submission': submission,
        }

    # 學習日誌
    journals = LearningJournal.query.filter_by(
        user_id=student.id, semester=SEMESTER
    ).order_by(LearningJournal.journal_number).all()
    journal_map = {j.journal_number: j for j in journals}

    return render_template('teacher/student_profile.html',
                           student=student,
                           task_submissions=task_submissions,
                           tasks=TASKS,
                           questionnaire_status=questionnaire_status,
                           journal_map=journal_map,
                           total_journals=5)


@app.route('/teacher/analytics')
@login_required
def teacher_analytics():
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))

    task_number = request.args.get('task', 1, type=int)
    task_def    = TASKS.get(task_number, {})
    subs        = TaskSubmission.query.filter_by(
        task_number=task_number, semester=SEMESTER).all()

    submissions_data = []
    for s in subs:
        ai_fb = s.ai_feedbacks.order_by(AIFeedback.created_at.desc()).first()
        # 整理反思回答供分析
        rq_map = {r.question_id: r.answer for r in s.reflection_responses}
        submissions_data.append({
            'student_id':   s.author.student_id,
            'class':        s.author.class_group,
            'task_version': s.task_version,
            'reflection_answers': rq_map,
            'checklist_completion': sum(
                1 for r in s.checklist_responses if r.checked
            ),
            'ai_scores': json.loads(ai_fb.scores)
                         if ai_fb and ai_fb.scores else {},
        })

    analysis = ''
    if submissions_data and app.config.get('ANTHROPIC_API_KEY'):
        analysis = ai_service.generate_teacher_analysis(submissions_data)

    return render_template('teacher/analytics.html',
                           task_number=task_number,
                           task_def=task_def,
                           submissions=subs,
                           analysis=analysis,
                           tasks=TASKS)


# ─── Teacher: Data Export (P7) ───────────────────────────────────────────────
#
# 匯出格式均為 UTF-8 with BOM 的 CSV（Excel 可直接開啟），
# 一律限教師存取。
#
# 端點：
#   GET /teacher/export/task/<task_number>     → 單一任務結構化回答
#   GET /teacher/export/questionnaire/<q_code> → 單份問卷 Likert 回答
#   GET /teacher/export/arcsa-paired           → ARCSA 前後測配對寬格式
#   GET /teacher/export/journals               → 全學生學習日誌
# ─────────────────────────────────────────────────────────────────────────────

def _csv_response(rows, fieldnames, filename):
    """將 list[dict] 轉為 CSV Response（UTF-8 BOM）"""
    output = io.StringIO()
    output.write('\ufeff')          # BOM → Excel 正確顯示中文
    writer = csv.DictWriter(output, fieldnames=fieldnames,
                            extrasaction='ignore', lineterminator='\r\n')
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        output.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.route('/teacher/export/task/<int:task_number>')
@login_required
def export_task(task_number):
    """匯出單一任務的全班結構化回答（一行一學生）"""
    if not current_user.is_teacher:
        flash('無教師權限。', 'error')
        return redirect(url_for('dashboard'))

    task_def = TASKS.get(task_number)
    if not task_def:
        flash('無效任務編號。', 'error')
        return redirect(url_for('teacher_dashboard'))

    subs = TaskSubmission.query.filter_by(
        task_number=task_number, semester=SEMESTER
    ).order_by(TaskSubmission.submitted_at).all()

    # ── 動態建立欄位名稱清單 ──────────────────────────────────────
    fieldnames = ['student_id', 'name', 'class_group',
                  'submitted_at', 'updated_at', 'task_version', 'status']

    # 提示問題：pq_t{n}_pq{m}
    for pq in task_def['prompt_questions']:
        fieldnames.append(f'pq_{pq["id"]}')

    # 自我檢核：cl_t{n}_cl{m}（0/1）+ 備註
    for cl in task_def['checklist_items']:
        fieldnames.append(f'cl_{cl["id"]}')
        fieldnames.append(f'cl_note_{cl["id"]}')
    fieldnames.append('checklist_score')      # 勾選比率 0–1

    # 當責反思：rq_t{n}_rq{m}
    for rq in task_def['reflection_questions']:
        fieldnames.append(f'rq_{rq["id"]}')

    # 產出文字：dv_text_t{n}_d{m}
    for dv in task_def['deliverables']:
        if dv.get('accept_text'):
            fieldnames.append(f'dv_text_{dv["id"]}')
        if dv.get('accept_file'):
            fieldnames.append(f'dv_file_{dv["id"]}')

    # AI 分數 & 教師評分
    fieldnames += ['ai_scores_json', 'teacher_score', 'teacher_published']

    # ── 組裝每一行 ────────────────────────────────────────────────
    rows = []
    for sub in subs:
        row = {
            'student_id':   sub.author.student_id,
            'name':         sub.author.name,
            'class_group':  sub.author.class_group,
            'submitted_at': sub.submitted_at.strftime('%Y-%m-%d %H:%M'),
            'updated_at':   sub.updated_at.strftime('%Y-%m-%d %H:%M'),
            'task_version': sub.task_version,
            'status':       sub.status,
        }

        pq_map = {r.question_id: r.answer  for r in sub.question_responses}
        cl_map = {r.item_id: r             for r in sub.checklist_responses}
        rq_map = {r.question_id: r.answer  for r in sub.reflection_responses}
        du_map = {du.deliverable_id: du    for du in sub.deliverable_uploads}

        for pq in task_def['prompt_questions']:
            row[f'pq_{pq["id"]}'] = pq_map.get(pq['id'], '')

        checked = 0
        for cl in task_def['checklist_items']:
            cr = cl_map.get(cl['id'])
            row[f'cl_{cl["id"]}'] = 1 if (cr and cr.checked) else 0
            row[f'cl_note_{cl["id"]}'] = (cr.note if cr else '')
            if cr and cr.checked:
                checked += 1
        total_cl = len(task_def['checklist_items'])
        row['checklist_score'] = round(checked / total_cl, 4) if total_cl else ''

        for rq in task_def['reflection_questions']:
            row[f'rq_{rq["id"]}'] = rq_map.get(rq['id'], '')

        for dv in task_def['deliverables']:
            du = du_map.get(dv['id'])
            if dv.get('accept_text'):
                row[f'dv_text_{dv["id"]}'] = (du.content if du else '')
            if dv.get('accept_file'):
                row[f'dv_file_{dv["id"]}'] = (du.file_name if du else '')

        ai_fb = sub.ai_feedbacks.order_by(AIFeedback.created_at.desc()).first()
        row['ai_scores_json'] = (ai_fb.scores if ai_fb else '')

        tr = sub.teacher_reviews.order_by(TeacherReview.reviewed_at.desc()).first()
        row['teacher_score']     = (tr.score     if tr else '')
        row['teacher_published'] = (1 if (tr and tr.published) else 0)

        rows.append(row)

    fname = f'task{task_number}_{SEMESTER}_{datetime.now().strftime("%Y%m%d")}.csv'
    return _csv_response(rows, fieldnames, fname)


@app.route('/teacher/export/questionnaire/<string:q_code>')
@login_required
def export_questionnaire(q_code):
    """匯出單份問卷的全班回答（一行一學生，每題一欄）"""
    if not current_user.is_teacher:
        flash('無教師權限。', 'error')
        return redirect(url_for('dashboard'))

    q = Questionnaire.query.filter_by(code=q_code).first_or_404()
    q_subs = QuestionnaireSubmission.query.filter_by(
        questionnaire_id=q.id
    ).order_by(QuestionnaireSubmission.submitted_at).all()

    # 欄位：學生基本 + 每題 item_code + 各構面平均
    fieldnames = ['student_id', 'name', 'class_group', 'submitted_at']
    items = sorted(q.items, key=lambda i: i.order)

    for item in items:
        fieldnames.append(item.item_code)

    # 計算各構面平均：找出所有 dimension
    dims = {}
    for item in items:
        if item.scale_type == 'likert5':
            dims.setdefault(item.dimension, []).append(item.item_code)
    for dim in dims:
        fieldnames.append(f'dim_avg_{dim}')

    rows = []
    for qs in q_subs:
        ans_map = {a.item_code: a.value for a in qs.answers}
        row = {
            'student_id':   qs.author.student_id,
            'name':         qs.author.name,
            'class_group':  qs.author.class_group,
            'submitted_at': qs.submitted_at.strftime('%Y-%m-%d %H:%M'),
        }
        for item in items:
            row[item.item_code] = ans_map.get(item.item_code, '')

        # 各構面 Likert 平均
        for dim, codes in dims.items():
            vals = []
            for c in codes:
                v = ans_map.get(c, '')
                try:
                    vals.append(float(v))
                except (ValueError, TypeError):
                    pass
            row[f'dim_avg_{dim}'] = round(sum(vals) / len(vals), 4) if vals else ''

        rows.append(row)

    fname = f'{q_code}_{SEMESTER}_{datetime.now().strftime("%Y%m%d")}.csv'
    return _csv_response(rows, fieldnames, fname)


@app.route('/teacher/export/arcsa-paired')
@login_required
def export_arcsa_paired():
    """
    匯出 ARCSA 前後測配對寬格式（一行一學生）。
    欄：student_id, class_group,
        pre_{item_code}, post_{item_code} (各 25 欄),
        pre_avg_{dim}, post_avg_{dim}, diff_avg_{dim} (各 5 構面),
        pre_total, post_total, diff_total
    """
    if not current_user.is_teacher:
        flash('無教師權限。', 'error')
        return redirect(url_for('dashboard'))

    q_pre  = Questionnaire.query.filter_by(code='arcsa_pre').first()
    q_post = Questionnaire.query.filter_by(code='arcsa_post').first()
    if not q_pre or not q_post:
        flash('ARCSA 問卷尚未 seed 至資料庫。', 'error')
        return redirect(url_for('teacher_dashboard'))

    items = sorted(q_pre.items, key=lambda i: i.order)   # 前後測共用同題
    dims  = {}
    for item in items:
        if item.scale_type == 'likert5':
            dims.setdefault(item.dimension, []).append(item.item_code)

    # ── 欄位清單 ──────────────────────────────────────────────────
    fieldnames = ['student_id', 'name', 'class_group']
    for item in items:
        fieldnames += [f'pre_{item.item_code}', f'post_{item.item_code}']
    for dim in dims:
        fieldnames += [f'pre_avg_{dim}', f'post_avg_{dim}', f'diff_avg_{dim}']
    fieldnames += ['pre_total', 'post_total', 'diff_total']

    # ── 蒐集資料 ──────────────────────────────────────────────────
    # 建立 user_id → {pre: {code: val}, post: {code: val}}
    data = {}

    def _load(q_obj, timepoint):
        for qs in QuestionnaireSubmission.query.filter_by(
                questionnaire_id=q_obj.id).all():
            uid = qs.user_id
            if uid not in data:
                data[uid] = {'user': qs.author, 'pre': {}, 'post': {}}
            ans_map = {a.item_code: a.value for a in qs.answers}
            data[uid][timepoint] = ans_map

    _load(q_pre,  'pre')
    _load(q_post, 'post')

    rows = []
    for uid, d in sorted(data.items(),
                         key=lambda x: x[1]['user'].student_id):
        user = d['user']
        row = {
            'student_id':  user.student_id,
            'name':        user.name,
            'class_group': user.class_group,
        }
        for item in items:
            row[f'pre_{item.item_code}']  = d['pre'].get(item.item_code, '')
            row[f'post_{item.item_code}'] = d['post'].get(item.item_code, '')

        pre_total = post_total = 0
        pre_count = post_count = 0
        for dim, codes in dims.items():
            pre_vals, post_vals = [], []
            for c in codes:
                try:
                    pre_vals.append(float(d['pre'].get(c, '')))
                except (ValueError, TypeError):
                    pass
                try:
                    post_vals.append(float(d['post'].get(c, '')))
                except (ValueError, TypeError):
                    pass
            pre_avg  = round(sum(pre_vals)  / len(pre_vals),  4) if pre_vals  else ''
            post_avg = round(sum(post_vals) / len(post_vals), 4) if post_vals else ''
            row[f'pre_avg_{dim}']  = pre_avg
            row[f'post_avg_{dim}'] = post_avg
            row[f'diff_avg_{dim}'] = (
                round(post_avg - pre_avg, 4)
                if pre_avg != '' and post_avg != '' else ''
            )
            if pre_avg  != '': pre_total  += pre_avg;  pre_count  += 1
            if post_avg != '': post_total += post_avg; post_count += 1

        row['pre_total']  = round(pre_total  / pre_count,  4) if pre_count  else ''
        row['post_total'] = round(post_total / post_count, 4) if post_count else ''
        row['diff_total'] = (
            round(row['post_total'] - row['pre_total'], 4)
            if row['pre_total'] != '' and row['post_total'] != '' else ''
        )
        rows.append(row)

    fname = f'arcsa_paired_{SEMESTER}_{datetime.now().strftime("%Y%m%d")}.csv'
    return _csv_response(rows, fieldnames, fname)


@app.route('/teacher/export/journals')
@login_required
def export_journals():
    """匯出全班學習日誌（一行一篇）"""
    if not current_user.is_teacher:
        flash('無教師權限。', 'error')
        return redirect(url_for('dashboard'))

    journals = LearningJournal.query.filter_by(semester=SEMESTER)\
        .order_by(LearningJournal.user_id, LearningJournal.journal_number).all()

    fieldnames = ['student_id', 'name', 'class_group',
                  'journal_number', 'week', 'submitted_at', 'updated_at',
                  'char_count', 'content']
    rows = []
    for lj in journals:
        rows.append({
            'student_id':     lj.author.student_id,
            'name':           lj.author.name,
            'class_group':    lj.author.class_group,
            'journal_number': lj.journal_number,
            'week':           lj.week,
            'submitted_at':   lj.submitted_at.strftime('%Y-%m-%d %H:%M'),
            'updated_at':     lj.updated_at.strftime('%Y-%m-%d %H:%M'),
            'char_count':     len(lj.content.replace(' ', '').replace('\n', '')),
            'content':        lj.content,
        })

    fname = f'journals_{SEMESTER}_{datetime.now().strftime("%Y%m%d")}.csv'
    return _csv_response(rows, fieldnames, fname)


# ─── File Serving ─────────────────────────────────────────────────────────────

@app.route('/uploads/<path:filename>')
@login_required
def uploaded_file(filename):
    # 路徑格式為 "<owner_user_id>/<safe_filename>"，由 submit_task() 寫入時固定
    parts = filename.split('/', 1)
    if len(parts) < 2 or not parts[0].isdigit():
        abort(404)
    owner_id = int(parts[0])
    if not current_user.is_teacher and current_user.id != owner_id:
        abort(403)
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/download/submission-file/<int:submission_id>/<string:deliverable_id>')
@login_required
def download_deliverable_file(submission_id, deliverable_id):
    sub = db.session.get(TaskSubmission, submission_id)
    if not sub:
        flash('找不到此提交。', 'error')
        return redirect(url_for('dashboard'))
    if not current_user.is_teacher and sub.user_id != current_user.id:
        flash('無權限。', 'error')
        return redirect(url_for('dashboard'))

    du = DeliverableUpload.query.filter_by(
        submission_id=submission_id,
        deliverable_id=deliverable_id
    ).first_or_404()

    if not du.file_path:
        flash('此產出沒有上傳檔案。', 'error')
        return redirect(url_for('view_task_submission', submission_id=submission_id))

    directory = os.path.dirname(du.file_path)
    filename  = os.path.basename(du.file_path)
    return send_from_directory(directory, filename,
                               download_name=du.file_name or filename)


# ─── Teacher Questionnaire Management ────────────────────────────────────────

@app.route('/teacher/questionnaires')
@login_required
def manage_questionnaires():
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))

    total_students = User.query.filter_by(role='student').count()
    questionnaires = Questionnaire.query.order_by(Questionnaire.id).all()

    q_stats = []
    for q in questionnaires:
        submitted = QuestionnaireSubmission.query.filter_by(
            questionnaire_id=q.id).count()
        q_stats.append({
            'q':         q,
            'submitted': submitted,
            'total':     total_students,
            'rate':      round(submitted / total_students * 100, 1) if total_students else 0,
        })

    return render_template('teacher/questionnaire_mgmt.html',
                           q_stats=q_stats,
                           total_students=total_students)


@app.route('/teacher/questionnaires/<int:q_id>/toggle', methods=['POST'])
@login_required
def toggle_questionnaire(q_id):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))

    q = db.session.get(Questionnaire, q_id)
    if not q:
        flash('找不到此問卷。', 'error')
        return redirect(url_for('manage_questionnaires'))

    q.is_active = not q.is_active
    db.session.commit()
    status = '已開放' if q.is_active else '已關閉'
    flash(f'《{q.name}》{status}。', 'success')
    return redirect(url_for('manage_questionnaires'))


@app.route('/teacher/questionnaires/<int:q_id>/results')
@login_required
def view_questionnaire_results(q_id):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))

    q = db.session.get(Questionnaire, q_id)
    if not q:
        flash('找不到此問卷。', 'error')
        return redirect(url_for('manage_questionnaires'))

    total_students = User.query.filter_by(role='student').count()
    submissions = (QuestionnaireSubmission.query
                   .filter_by(questionnaire_id=q.id)
                   .order_by(QuestionnaireSubmission.submitted_at)
                   .all())
    submitted_ids = {s.user_id for s in submissions}

    # 各題平均分數（Likert 題）
    item_stats = []
    for item in q.items:
        answers = (QuestionnaireAnswer.query
                   .join(QuestionnaireSubmission)
                   .filter(QuestionnaireSubmission.questionnaire_id == q.id,
                           QuestionnaireAnswer.item_code == item.item_code)
                   .all())
        values = [int(a.value) for a in answers if a.value and a.value.isdigit()]
        item_stats.append({
            'item':   item,
            'mean':   round(sum(values) / len(values), 2) if values else None,
            'n':      len(values),
        })

    # 學生填答狀況
    all_students = User.query.filter_by(role='student')\
        .order_by(User.class_group, User.student_id).all()
    student_rows = []
    sub_by_uid = {s.user_id: s for s in submissions}
    for stu in all_students:
        student_rows.append({
            'student': stu,
            'submission': sub_by_uid.get(stu.id),
        })

    return render_template('teacher/questionnaire_results.html',
                           q=q,
                           item_stats=item_stats,
                           student_rows=student_rows,
                           total_students=total_students,
                           submitted_count=len(submissions))


@app.route('/teacher/questionnaires/<int:q_id>/student/<int:uid>')
@login_required
def view_student_questionnaire_response(q_id, uid):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))

    q = db.session.get(Questionnaire, q_id)
    student = db.session.get(User, uid)
    if not q or not student:
        flash('找不到資料。', 'error')
        return redirect(url_for('manage_questionnaires'))

    sub = QuestionnaireSubmission.query.filter_by(
        questionnaire_id=q.id, user_id=uid).first()
    if not sub:
        flash('此學生尚未填答。', 'warning')
        return redirect(url_for('view_questionnaire_results', q_id=q_id))

    existing_answers = {a.item_code: a.value for a in sub.answers}

    return render_template('student/questionnaire.html',
                           q=q,
                           existing=sub,
                           existing_answers=existing_answers)


# ─── API ──────────────────────────────────────────────────────────────────────

@app.route('/api/event/competency-radar-viewed', methods=['POST'])
@login_required
def api_competency_radar_viewed():
    if current_user.is_teacher:
        return {'ok': False}, 403
    # 同一 user 同一 semester 同一日只記一次
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    exists = LearningEvent.query.filter(
        LearningEvent.user_id == current_user.id,
        LearningEvent.event_type == 'competency_radar_viewed',
        LearningEvent.created_at >= today_start,
    ).first()
    if exists:
        return {'ok': True, 'deduped': True}
    db.session.add(LearningEvent(
        user_id     = current_user.id,
        event_type  = 'competency_radar_viewed',
        entity_type = '',
        payload_json= json.dumps({'semester': SEMESTER}),
    ))
    db.session.commit()
    return {'ok': True}


@app.route('/api/event/ai-feedback-viewed', methods=['POST'])
@login_required
def api_ai_feedback_viewed():
    if current_user.is_teacher:
        return {'ok': False}, 403
    data = request.get_json(silent=True) or {}
    try:
        submission_id = int(data.get('submission_id'))
    except (TypeError, ValueError):
        return {'ok': False, 'error': 'invalid_submission_id'}, 400
    try:
        feedback_id = int(data.get('feedback_id')) if data.get('feedback_id') else None
    except (TypeError, ValueError):
        feedback_id = None

    # Ownership check：學生只能標記自己的提交
    sub = db.session.get(TaskSubmission, submission_id)
    if not sub or sub.user_id != current_user.id:
        return {'ok': False, 'error': 'not_owner'}, 403

    # 同一 (user, submission, feedback) 已存在則略過，避免刷頁面重複寫。
    # 包 } 或 , 確保 12 不會 prefix-match 到 123。
    exists_q = LearningEvent.query.filter_by(
        user_id=current_user.id,
        event_type='ai_feedback_viewed',
        entity_type='task_submission',
        entity_id=submission_id,
    )
    if feedback_id is not None:
        exists_q = exists_q.filter(
            LearningEvent.payload_json.like(f'%"feedback_id": {feedback_id}}}%')
        )
    if exists_q.first():
        return {'ok': True, 'deduped': True}

    db.session.add(LearningEvent(
        user_id     = current_user.id,
        event_type  = 'ai_feedback_viewed',
        entity_type = 'task_submission',
        entity_id   = submission_id,
        payload_json= json.dumps({'feedback_id': feedback_id}),
    ))
    db.session.commit()
    return {'ok': True}


@app.route('/api/regenerate-feedback/<int:submission_id>', methods=['POST'])
@login_required
def regenerate_feedback(submission_id):
    sub = db.session.get(TaskSubmission, submission_id)
    if not sub:
        return jsonify({'error': '找不到提交'}), 404
    if not current_user.is_teacher and sub.user_id != current_user.id:
        return jsonify({'error': '無權限'}), 403

    # 沒設 API key 直接拒絕；否則會寫入 fallback feedback + 假 ai_feedback_received event 污染 trigger
    if not app.config.get('ANTHROPIC_API_KEY'):
        return jsonify({'error': 'AI 功能尚未啟用'}), 503

    task_def = TASKS.get(sub.task_number, {})
    text     = _build_submission_text_for_ai(sub, task_def)
    result   = ai_service.generate_instant_feedback(
        sub.task_number, 'structured', text, sub.author.name
    )
    ai_fb = AIFeedback(
        task_submission_id = sub.id,
        feedback_type      = 'overall',
        feedback           = result.get('feedback', ''),
        scores             = json.dumps(result.get('scores', {}), ensure_ascii=False),
        model_used         = 'claude-sonnet-4-20250514'
    )
    db.session.add(ai_fb)
    db.session.flush()  # 取得 ai_fb.id
    # user_id 用提交者，因教師也可觸發 regenerate
    db.session.add(LearningEvent(
        user_id      = sub.user_id,
        event_type   = 'ai_feedback_received',
        entity_type  = 'task_submission',
        entity_id    = sub.id,
        payload_json = json.dumps({'ai_feedback_id': ai_fb.id}, ensure_ascii=False),
    ))
    db.session.commit()
    return jsonify({'success': True, 'feedback': result.get('feedback', '')})


# ─── AI Tutor API ────────────────────────────────────────────────────────────

@app.route('/api/tutor/chat', methods=['POST'])
@login_required
def tutor_chat():
    data = request.get_json(silent=True) or {}
    user_message = (data.get('message') or '').strip()
    if not user_message:
        return jsonify({'error': '請輸入問題'}), 400

    page_context = data.get('page_context', '')

    # Load or create conversation
    conv = TutorConversation.query.filter_by(user_id=current_user.id)\
        .order_by(TutorConversation.updated_at.desc()).first()
    if not conv:
        conv = TutorConversation(user_id=current_user.id, messages='[]')
        db.session.add(conv)
        db.session.flush()

    try:
        messages = json.loads(conv.messages or '[]')
    except (json.JSONDecodeError, TypeError):
        messages = []

    # Sliding window: last 5 rounds (10 messages)
    recent = messages[-10:] if len(messages) > 10 else messages

    # Call ai-tutor-service
    try:
        resp = http_requests.post(
            f"{app.config['AI_TUTOR_URL']}/api/ai-tutor/chat",
            json={
                'message': user_message,
                'system': 'eagle-lms',
                'conversation_history': recent,
                'page_context': page_context,
            },
            headers={'X-Service-Key': app.config['AI_TUTOR_SERVICE_KEY']},
            timeout=30,
        )
        if resp.status_code != 200:
            return jsonify({'error': 'AI 服務暫時無法使用'}), 502
        try:
            result = resp.json()
        except ValueError:
            return jsonify({'error': 'AI 服務回傳格式錯誤'}), 502
    except Exception:
        return jsonify({'error': 'AI 服務連線失敗'}), 502

    answer = (result or {}).get('answer')
    if not isinstance(answer, str) or not answer.strip():
        return jsonify({'error': 'AI 服務未回傳有效內容'}), 502

    # Append both messages to conversation
    messages.append({'role': 'user', 'content': user_message})
    messages.append({'role': 'assistant', 'content': answer})
    conv.messages = json.dumps(messages, ensure_ascii=False)
    conv.updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify(result)


@app.route('/api/tutor/history', methods=['GET'])
@login_required
def tutor_history():
    conv = TutorConversation.query.filter_by(user_id=current_user.id)\
        .order_by(TutorConversation.updated_at.desc()).first()
    if not conv:
        return jsonify({'messages': []})
    return jsonify({'messages': json.loads(conv.messages)})


@app.route('/api/tutor/new', methods=['POST'])
@login_required
def tutor_new_conversation():
    conv = TutorConversation(user_id=current_user.id, messages='[]')
    db.session.add(conv)
    db.session.commit()
    return jsonify({'success': True})


# ─── Messages ─────────────────────────────────────────────────────────────────

def _student_visible_messages(user):
    msgs = Message.query.filter(
        _student_msg_filter(user)
    ).order_by(Message.created_at.desc()).all()
    read_ids = {r.message_id for r in MessageRead.query.filter_by(user_id=user.id).all()}
    return msgs, read_ids


@app.route('/messages')
@login_required
def messages():
    if current_user.is_teacher:
        return redirect(url_for('teacher_messages'))
    msgs, read_ids = _student_visible_messages(current_user)
    broadcasts = [m for m in msgs if m.recipient_id is None]
    personal   = [m for m in msgs if m.recipient_id == current_user.id]
    return render_template('student/messages.html',
                           broadcasts=broadcasts, personal=personal,
                           read_ids=read_ids)


@app.route('/messages/send', methods=['POST'])
@login_required
def student_send_message():
    if current_user.is_teacher:
        return redirect(url_for('teacher_messages'))
    body    = request.form.get('body', '').strip()
    subject = request.form.get('subject', '').strip()
    if not body:
        flash('訊息內容不能為空。', 'error')
        return redirect(url_for('messages'))
    teacher = User.query.filter_by(role='teacher').first()
    if not teacher:
        flash('找不到教師帳號。', 'error')
        return redirect(url_for('messages'))
    msg = Message(sender_id=current_user.id, recipient_id=teacher.id,
                  scope='personal', subject=subject, body=body)
    db.session.add(msg)
    db.session.commit()
    flash('訊息已送出。', 'success')
    return redirect(url_for('messages'))


@app.route('/messages/<int:msg_id>/read', methods=['POST'])
@login_required
def mark_message_read(msg_id):
    if not MessageRead.query.filter_by(message_id=msg_id, user_id=current_user.id).first():
        db.session.add(MessageRead(message_id=msg_id, user_id=current_user.id))
        db.session.commit()
    return jsonify({'ok': True})


@app.route('/teacher/messages')
@login_required
def teacher_messages():
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    teacher_id = current_user.id

    # 公告（老師發出、無特定收件人）
    announcements = Message.query.filter(
        Message.sender_id == teacher_id,
        Message.recipient_id == None
    ).order_by(Message.created_at.desc()).all()

    # 私訊對話列表：找出所有與老師有私訊往來的學生
    personal_msgs = Message.query.filter(
        or_(
            and_(Message.sender_id == teacher_id, Message.recipient_id != None),
            Message.recipient_id == teacher_id
        )
    ).order_by(Message.created_at.desc()).all()

    unread_subq = db.session.query(MessageRead.message_id).filter_by(user_id=teacher_id)
    seen, conversations = set(), []
    for msg in personal_msgs:
        sid = msg.recipient_id if msg.sender_id == teacher_id else msg.sender_id
        if sid in seen:
            continue
        seen.add(sid)
        student = db.session.get(User, sid)
        if not student:
            continue
        unread = Message.query.filter(
            Message.sender_id == sid,
            Message.recipient_id == teacher_id,
            Message.id.notin_(unread_subq)
        ).count()
        conversations.append({'student': student, 'last_msg': msg, 'unread': unread})

    students = User.query.filter_by(role='student', status='active')\
        .order_by(User.class_group, User.student_id).all()
    preset_to = request.args.get('to', type=int)
    return render_template('teacher/messages.html',
                           conversations=conversations,
                           announcements=announcements,
                           students=students,
                           preset_to=preset_to)


@app.route('/teacher/messages/send', methods=['POST'])
@login_required
def teacher_send_message():
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    body         = request.form.get('body', '').strip()
    subject      = request.form.get('subject', '').strip()
    scope        = request.form.get('scope', 'all')
    recipient_id = request.form.get('recipient_id', type=int)
    if not body:
        flash('訊息內容不能為空。', 'error')
        return redirect(url_for('teacher_messages'))
    if scope == 'personal' and recipient_id:
        msg = Message(sender_id=current_user.id, recipient_id=recipient_id,
                      scope='personal', subject=subject, body=body)
    else:
        msg = Message(sender_id=current_user.id, recipient_id=None,
                      scope=scope, subject=subject, body=body)
    db.session.add(msg)
    db.session.commit()
    flash('訊息已發送。', 'success')
    return redirect(url_for('teacher_messages'))


@app.route('/teacher/messages/<int:msg_id>/reply', methods=['POST'])
@login_required
def teacher_reply_message(msg_id):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    original = db.session.get(Message, msg_id)
    if not original:
        flash('找不到原始訊息。', 'error')
        return redirect(url_for('teacher_messages'))
    body    = request.form.get('body', '').strip()
    subject = request.form.get('subject', '').strip()
    if not body:
        flash('回覆內容不能為空。', 'error')
        return redirect(url_for('teacher_messages'))
    reply = Message(sender_id=current_user.id, recipient_id=original.sender_id,
                    scope='personal',
                    subject=subject or (f'Re: {original.subject}' if original.subject else '回覆'),
                    body=body, reply_to_id=msg_id)
    db.session.add(reply)
    if not MessageRead.query.filter_by(message_id=msg_id, user_id=current_user.id).first():
        db.session.add(MessageRead(message_id=msg_id, user_id=current_user.id))
    db.session.commit()
    flash('回覆已送出。', 'success')
    return redirect(url_for('teacher_messages'))


@app.route('/teacher/messages/<int:msg_id>/delete', methods=['POST'])
@login_required
def teacher_delete_message(msg_id):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    msg = db.session.get(Message, msg_id)
    if not msg:
        flash('找不到此訊息。', 'error')
    elif msg.sender_id != current_user.id and msg.recipient_id != current_user.id:
        flash('無權限刪除此訊息。', 'error')
    else:
        db.session.delete(msg)
        db.session.commit()
        flash('訊息已刪除。', 'success')
    return redirect(url_for('teacher_messages'))


@app.route('/api/messages/thread/<int:student_id>')
@login_required
def api_message_thread(student_id):
    if not current_user.is_teacher:
        return jsonify({'error': '無權限'}), 403
    student = db.session.get(User, student_id)
    if not student or student.role != 'student':
        return jsonify({'error': '找不到此學生'}), 404

    teacher_id = current_user.id
    msgs = Message.query.filter(
        or_(
            and_(Message.sender_id == teacher_id, Message.recipient_id == student_id),
            and_(Message.sender_id == student_id, Message.recipient_id == teacher_id)
        )
    ).order_by(Message.created_at.asc()).all()

    read_set = {r.message_id for r in MessageRead.query.filter_by(user_id=teacher_id).all()}
    newly_read = [m.id for m in msgs if m.sender_id == student_id and m.id not in read_set]
    for mid in newly_read:
        db.session.add(MessageRead(message_id=mid, user_id=teacher_id))
    if newly_read:
        db.session.commit()

    thread = [{
        'id':          m.id,
        'is_teacher':  m.sender_id == teacher_id,
        'sender_name': m.sender.name,
        'body':        m.body,
        'created_at':  m.created_at.strftime('%Y-%m-%d %H:%M'),
    } for m in msgs]

    return jsonify({
        'student_id':        student_id,
        'student_name':      student.name,
        'student_sid':       student.student_id,
        'thread':            thread,
        'newly_read_count':  len(newly_read),
    })


@app.route('/api/messages/thread/<int:student_id>/reply', methods=['POST'])
@login_required
def api_thread_reply(student_id):
    if not current_user.is_teacher:
        return jsonify({'error': '無權限'}), 403
    student = db.session.get(User, student_id)
    if not student or student.role != 'student':
        return jsonify({'error': '找不到此學生'}), 404

    data = request.get_json() or {}
    body = (data.get('body') or '').strip()
    if not body:
        return jsonify({'error': '訊息內容不能為空'}), 400

    msg = Message(
        sender_id=current_user.id,
        recipient_id=student_id,
        scope='personal',
        subject='',
        body=body,
    )
    db.session.add(msg)
    db.session.commit()

    return jsonify({
        'id':          msg.id,
        'is_teacher':  True,
        'sender_name': current_user.name,
        'body':        msg.body,
        'created_at':  msg.created_at.strftime('%Y-%m-%d %H:%M'),
    })


# ─── Workshop Module (v2.5.0) ────────────────────────────────────────────────
# 工作坊模組：報名 → 簽到 → 反思的當責行為鏈
# =============================================================================

import random as _random
from datetime import timedelta, timezone as _timezone

_TAIPEI = _timezone(timedelta(hours=8))

WORKSHOP_TYPE_LABELS = {
    'system_ops':  '系統操作工作坊',
    'site_visit':  '工地參觀',
    'expert_talk': '業師講座',
    'other':       '其他',
}


def _now():
    """回傳台北本地時間（naive datetime），與表單輸入一致。
    Railway 伺服器跑在 UTC，必須明確指定 Asia/Taipei (+8) 後再去掉 tzinfo。"""
    return datetime.now(_TAIPEI).replace(tzinfo=None)


def _generate_checkin_code():
    """產生 4 位數字簽到碼（避免 0000）。"""
    return f'{_random.randint(1, 9999):04d}'


def _workshop_status_for(participation, workshop, now=None):
    """回傳單場工作坊對單一學生的當前狀態，供 template 顯示徽章。

    回傳：
      'not_registered'      未報名
      'cancelled'           已取消報名
      'registered'          已報名（活動尚未開始或進行中）
      'attended'            已簽到（反思尚未填寫）
      'reflection_done'     反思已填
      'reflection_overdue'  已簽到但反思已過期
      'closed'              工作坊已 cancelled / 已過期未參與
    """
    if now is None:
        now = _now()

    if workshop.status == 'cancelled':
        return 'closed'

    if not participation or participation.registered_at is None:
        # 從未建立或從未真正報名過
        if now > workshop.ends_at:
            return 'closed'
        return 'not_registered'

    if participation.cancelled_at:
        return 'cancelled'

    if participation.reflection_submitted_at:
        return 'reflection_done'

    if participation.checkin_at:
        if now > workshop.reflection_due_at:
            return 'reflection_overdue'
        return 'attended'

    # 已報名但未簽到
    if now > workshop.ends_at:
        return 'closed'
    return 'registered'


def _can_register(workshop, user, now=None):
    """回傳 (allowed: bool, reason: str)。"""
    if now is None:
        now = _now()
    if workshop.status != 'published':
        return False, '此工作坊尚未開放報名。'
    if workshop.semester != SEMESTER:
        return False, '此工作坊不屬於本學期。'
    if workshop.registration_opens_at and now < workshop.registration_opens_at:
        return False, '報名尚未開放。'
    if workshop.registration_closes_at and now > workshop.registration_closes_at:
        return False, '報名已截止。'
    if now > workshop.starts_at:
        return False, '工作坊已開始，無法再報名。'
    if workshop.capacity is not None:
        active_count = WorkshopParticipation.query.filter(
            WorkshopParticipation.workshop_id == workshop.id,
            WorkshopParticipation.registered_at != None,
            WorkshopParticipation.cancelled_at == None,
        ).count()
        if active_count >= workshop.capacity:
            return False, '名額已滿。'
    return True, ''


def _can_checkin(workshop, participation, now=None):
    if now is None:
        now = _now()
    if not participation or participation.registered_at is None or participation.cancelled_at:
        return False, '尚未報名此工作坊，無法簽到。'
    if participation.checkin_at:
        return False, '已完成簽到。'
    if now < workshop.checkin_window_starts_at:
        return False, '簽到時間尚未開始。'
    if now > workshop.checkin_window_ends_at:
        return False, '簽到時間已結束。'
    return True, ''


def _can_submit_reflection(workshop, participation, now=None):
    if now is None:
        now = _now()
    if not participation or not participation.checkin_at:
        return False, '需要先完成簽到才能填寫反思。'
    if now > workshop.reflection_due_at:
        return False, '反思填寫期限已過。'
    return True, ''


def _get_or_create_participation(workshop_id, user_id):
    """確保 (workshop_id, user_id) 唯一性下取得或建立 participation。"""
    p = WorkshopParticipation.query.filter_by(
        workshop_id=workshop_id, user_id=user_id).first()
    if p is None:
        p = WorkshopParticipation(workshop_id=workshop_id, user_id=user_id)
        db.session.add(p)
    return p


def _parse_dt(value):
    """將表單字串解析為 datetime，失敗回傳 None。
    表單欄位採用 HTML datetime-local 格式：'YYYY-MM-DDTHH:MM'。
    """
    if not value:
        return None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _fmt_dt(dt):
    """供 datetime-local input 預填使用。"""
    if not dt:
        return ''
    return dt.strftime('%Y-%m-%dT%H:%M')


# ─── Workshop: Teacher Routes ────────────────────────────────────────────────

@app.route('/teacher/workshops')
@login_required
def teacher_workshop_list():
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    workshops = Workshop.query.filter_by(semester=SEMESTER).order_by(
        Workshop.starts_at.desc()).all()
    # 帶上各場統計
    items = []
    for w in workshops:
        registered = WorkshopParticipation.query.filter(
            WorkshopParticipation.workshop_id == w.id,
            WorkshopParticipation.registered_at != None,
            WorkshopParticipation.cancelled_at == None,
        ).count()
        attended = WorkshopParticipation.query.filter(
            WorkshopParticipation.workshop_id == w.id,
            WorkshopParticipation.checkin_at != None,
        ).count()
        reflected = WorkshopParticipation.query.filter(
            WorkshopParticipation.workshop_id == w.id,
            WorkshopParticipation.reflection_submitted_at != None,
        ).count()
        items.append({
            'workshop':   w,
            'registered': registered,
            'attended':   attended,
            'reflected':  reflected,
        })
    return render_template('teacher/workshop_list.html',
                           items=items,
                           type_labels=WORKSHOP_TYPE_LABELS)


@app.route('/teacher/workshops/new', methods=['GET', 'POST'])
@login_required
def teacher_new_workshop():
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))

    if request.method == 'GET':
        return render_template('teacher/workshop_form.html',
                               workshop=None,
                               type_labels=WORKSHOP_TYPE_LABELS,
                               fmt_dt=_fmt_dt)

    # POST
    title    = request.form.get('title', '').strip()
    wtype    = request.form.get('type', 'other').strip()
    desc     = request.form.get('description', '').strip()
    location = request.form.get('location', '').strip()
    starts   = _parse_dt(request.form.get('starts_at'))
    ends     = _parse_dt(request.form.get('ends_at'))
    cap_raw  = request.form.get('capacity', '').strip()
    capacity = int(cap_raw) if cap_raw.isdigit() else None
    reg_open  = _parse_dt(request.form.get('registration_opens_at'))
    reg_close = _parse_dt(request.form.get('registration_closes_at'))
    reflection_due = _parse_dt(request.form.get('reflection_due_at'))

    # 驗證
    if not title or not starts or not ends:
        flash('標題、開始與結束時間為必填。', 'error')
        return redirect(url_for('teacher_new_workshop'))
    if starts >= ends:
        flash('開始時間必須早於結束時間。', 'error')
        return redirect(url_for('teacher_new_workshop'))
    if reg_close and reg_close > starts:
        flash('報名截止時間不能晚於工作坊開始時間。', 'error')
        return redirect(url_for('teacher_new_workshop'))
    # 預設值
    if not reflection_due:
        reflection_due = ends + timedelta(hours=48)
    if reflection_due <= ends:
        flash('反思截止時間必須晚於工作坊結束時間。', 'error')
        return redirect(url_for('teacher_new_workshop'))

    checkin_start = starts - timedelta(minutes=10)
    checkin_end   = ends + timedelta(minutes=10)

    w = Workshop(
        title=title,
        type=wtype if wtype in WORKSHOP_TYPE_LABELS else 'other',
        description=desc,
        location=location,
        starts_at=starts,
        ends_at=ends,
        capacity=capacity,
        registration_opens_at=reg_open,
        registration_closes_at=reg_close,
        checkin_code=_generate_checkin_code(),
        checkin_window_starts_at=checkin_start,
        checkin_window_ends_at=checkin_end,
        reflection_due_at=reflection_due,
        semester=SEMESTER,
        status='draft',
        created_by=current_user.id,
    )
    db.session.add(w)
    db.session.commit()
    flash('工作坊已建立（草稿狀態）。請進入詳情頁發布。', 'success')
    return redirect(url_for('teacher_workshop_detail', wid=w.id))


@app.route('/teacher/workshops/<int:wid>')
@login_required
def teacher_workshop_detail(wid):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    w = db.session.get(Workshop, wid)
    if not w:
        flash('找不到此工作坊。', 'error')
        return redirect(url_for('teacher_workshop_list'))

    parts = WorkshopParticipation.query.filter_by(workshop_id=wid).all()
    rows = []
    for p in parts:
        rows.append({
            'participation': p,
            'student':       p.user,
            'status':        _workshop_status_for(p, w),
        })
    # 排序：已簽到優先、再依姓名
    rows.sort(key=lambda r: (r['participation'].checkin_at is None,
                             r['student'].name))

    stats = {
        'registered': sum(1 for p in parts
                          if p.registered_at and not p.cancelled_at),
        'attended':   sum(1 for p in parts if p.checkin_at),
        'reflected':  sum(1 for p in parts if p.reflection_submitted_at),
        'cancelled':  sum(1 for p in parts if p.cancelled_at),
    }

    return render_template('teacher/workshop_detail.html',
                           w=w, rows=rows, stats=stats,
                           type_labels=WORKSHOP_TYPE_LABELS)


@app.route('/teacher/workshops/<int:wid>/edit', methods=['GET', 'POST'])
@login_required
def teacher_edit_workshop(wid):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    w = db.session.get(Workshop, wid)
    if not w:
        flash('找不到此工作坊。', 'error')
        return redirect(url_for('teacher_workshop_list'))

    if request.method == 'GET':
        return render_template('teacher/workshop_form.html',
                               workshop=w,
                               type_labels=WORKSHOP_TYPE_LABELS,
                               fmt_dt=_fmt_dt)

    # POST：可能是「儲存編輯」、「發布」、「取消」、「重產簽到碼」
    action = request.form.get('action', 'save')

    if action == 'publish':
        w.status = 'published'
        db.session.commit()
        flash('工作坊已發布，學生可開始報名。', 'success')
        return redirect(url_for('teacher_workshop_detail', wid=w.id))

    if action == 'cancel_workshop':
        w.status = 'cancelled'
        db.session.commit()
        flash('工作坊已取消。', 'warning')
        return redirect(url_for('teacher_workshop_detail', wid=w.id))

    if action == 'regenerate_code':
        w.checkin_code = _generate_checkin_code()
        db.session.commit()
        flash(f'簽到碼已重新產生：{w.checkin_code}', 'success')
        return redirect(url_for('teacher_workshop_detail', wid=w.id))

    # 一般儲存
    title    = request.form.get('title', '').strip()
    wtype    = request.form.get('type', 'other').strip()
    desc     = request.form.get('description', '').strip()
    location = request.form.get('location', '').strip()
    starts   = _parse_dt(request.form.get('starts_at'))
    ends     = _parse_dt(request.form.get('ends_at'))
    cap_raw  = request.form.get('capacity', '').strip()
    capacity = int(cap_raw) if cap_raw.isdigit() else None
    reg_open  = _parse_dt(request.form.get('registration_opens_at'))
    reg_close = _parse_dt(request.form.get('registration_closes_at'))
    reflection_due = _parse_dt(request.form.get('reflection_due_at'))

    if not title or not starts or not ends:
        flash('標題、開始與結束時間為必填。', 'error')
        return redirect(url_for('teacher_edit_workshop', wid=w.id))
    if starts >= ends:
        flash('開始時間必須早於結束時間。', 'error')
        return redirect(url_for('teacher_edit_workshop', wid=w.id))
    if reg_close and reg_close > starts:
        flash('報名截止時間不能晚於工作坊開始時間。', 'error')
        return redirect(url_for('teacher_edit_workshop', wid=w.id))
    if not reflection_due:
        reflection_due = ends + timedelta(hours=48)
    if reflection_due <= ends:
        flash('反思截止時間必須晚於工作坊結束時間。', 'error')
        return redirect(url_for('teacher_edit_workshop', wid=w.id))

    w.title = title
    w.type = wtype if wtype in WORKSHOP_TYPE_LABELS else 'other'
    w.description = desc
    w.location = location
    w.starts_at = starts
    w.ends_at = ends
    w.capacity = capacity
    w.registration_opens_at = reg_open
    w.registration_closes_at = reg_close
    w.reflection_due_at = reflection_due
    w.checkin_window_starts_at = starts - timedelta(minutes=10)
    w.checkin_window_ends_at   = ends + timedelta(minutes=10)
    db.session.commit()
    flash('工作坊已更新。', 'success')
    return redirect(url_for('teacher_workshop_detail', wid=w.id))


@app.route('/teacher/workshops/<int:wid>/attendance', methods=['GET', 'POST'])
@login_required
def teacher_attendance(wid):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    w = db.session.get(Workshop, wid)
    if not w:
        flash('找不到此工作坊。', 'error')
        return redirect(url_for('teacher_workshop_list'))

    if request.method == 'POST':
        # 表單欄位 'attend_<user_id>' 為 'on' 表示已出席
        # 補登：只把「未簽到」改為「已簽到」；不會把已簽到的移除（避免誤刪 self_code 紀錄）
        students_to_mark = request.form.getlist('attend')  # list of user_id strings
        marked = 0
        for sid_str in students_to_mark:
            try:
                sid = int(sid_str)
            except ValueError:
                continue
            p = _get_or_create_participation(w.id, sid)
            if p.registered_at is None:
                p.registered_at = _now()  # 教師補登 = 視為已報名
                p.pre_goal = p.pre_goal or '（教師補登出席，無課前目標紀錄）'
            if not p.checkin_at:
                p.checkin_at = _now()
                p.checkin_method = 'teacher_manual'
                marked += 1
        db.session.commit()
        flash(f'補登完成：本次新增 {marked} 筆出席紀錄。', 'success')
        return redirect(url_for('teacher_attendance', wid=w.id))

    # GET：列出全部學生（僅 student role 且 active），標出已簽到者
    students = User.query.filter_by(role='student', status='active').order_by(
        User.class_group, User.student_id).all()
    parts_map = {p.user_id: p for p in WorkshopParticipation.query.filter_by(
        workshop_id=wid).all()}
    rows = []
    for s in students:
        p = parts_map.get(s.id)
        rows.append({
            'student':       s,
            'participation': p,
            'attended':      bool(p and p.checkin_at),
            'method':        p.checkin_method if p else '',
        })
    return render_template('teacher/workshop_attendance.html',
                           w=w, rows=rows)


@app.route('/teacher/workshops/<int:wid>/reflections')
@login_required
def teacher_workshop_reflections(wid):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    w = db.session.get(Workshop, wid)
    if not w:
        flash('找不到此工作坊。', 'error')
        return redirect(url_for('teacher_workshop_list'))
    parts = WorkshopParticipation.query.filter(
        WorkshopParticipation.workshop_id == wid,
        WorkshopParticipation.reflection_submitted_at != None,
    ).all()
    parts.sort(key=lambda p: (p.user.class_group, p.user.student_id))
    return render_template('teacher/workshop_reflections.html',
                           w=w, parts=parts)


# ─── Workshop: Student Routes ────────────────────────────────────────────────

@app.route('/workshops')
@login_required
def workshop_list():
    if current_user.is_teacher:
        return redirect(url_for('teacher_workshop_list'))
    now = _now()
    workshops = Workshop.query.filter(
        Workshop.semester == SEMESTER,
        Workshop.status == 'published',
    ).order_by(Workshop.starts_at.asc()).all()

    parts_map = {p.workshop_id: p for p in WorkshopParticipation.query.filter_by(
        user_id=current_user.id).all()}

    items = []
    for w in workshops:
        p = parts_map.get(w.id)
        items.append({
            'workshop': w,
            'participation': p,
            'status': _workshop_status_for(p, w, now),
        })
    return render_template('student/workshop_list.html',
                           items=items, now=now,
                           type_labels=WORKSHOP_TYPE_LABELS)


@app.route('/workshops/<int:wid>')
@login_required
def view_workshop(wid):
    if current_user.is_teacher:
        return redirect(url_for('teacher_workshop_detail', wid=wid))
    w = db.session.get(Workshop, wid)
    if not w or w.semester != SEMESTER or w.status not in ('published', 'completed', 'cancelled'):
        flash('找不到此工作坊或尚未開放。', 'error')
        return redirect(url_for('workshop_list'))
    p = WorkshopParticipation.query.filter_by(
        workshop_id=wid, user_id=current_user.id).first()
    now = _now()
    can_reg, reg_msg = _can_register(w, current_user, now)
    return render_template('student/workshop_detail.html',
                           w=w, p=p, now=now,
                           status=_workshop_status_for(p, w, now),
                           can_register=can_reg,
                           register_block_reason=reg_msg,
                           type_labels=WORKSHOP_TYPE_LABELS)


@app.route('/workshops/<int:wid>/register', methods=['POST'])
@login_required
def register_workshop(wid):
    if current_user.is_teacher:
        return redirect(url_for('dashboard'))
    w = db.session.get(Workshop, wid)
    if not w:
        flash('找不到此工作坊。', 'error')
        return redirect(url_for('workshop_list'))

    pre_goal = request.form.get('pre_goal', '').strip()
    if len(pre_goal) < 10:
        flash('請填寫課前自設目標（至少 10 字）——這是工作坊當責設計的一部分。', 'error')
        return redirect(url_for('view_workshop', wid=wid))

    allowed, reason = _can_register(w, current_user)
    # 若是「重新報名」（之前 cancelled），允許繼續
    p = WorkshopParticipation.query.filter_by(
        workshop_id=wid, user_id=current_user.id).first()
    is_recovering = bool(p and p.cancelled_at)
    if not allowed and not is_recovering:
        flash(reason, 'error')
        return redirect(url_for('view_workshop', wid=wid))

    if p is None:
        p = WorkshopParticipation(workshop_id=wid, user_id=current_user.id)
        db.session.add(p)
    p.registered_at = _now()
    p.pre_goal = pre_goal
    p.cancelled_at = None
    p.cancel_reason = ''
    db.session.commit()

    # 發送報名確認訊息（複用既有 Message 系統）
    try:
        msg_body = (
            f'您已成功報名工作坊「{w.title}」。\n\n'
            f'活動時間：{w.starts_at.strftime("%Y-%m-%d %H:%M")} – '
            f'{w.ends_at.strftime("%H:%M")}\n'
            f'地點：{w.location or "（待公告）"}\n\n'
            f'請於活動開始後在現場輸入簽到碼完成簽到。'
            f'活動結束後 48 小時內請填寫反思。'
        )
        msg = Message(
            sender_id=w.created_by,
            recipient_id=current_user.id,
            scope='personal',
            subject=f'[工作坊報名確認] {w.title}',
            body=msg_body,
        )
        db.session.add(msg)
        db.session.commit()
    except Exception:
        db.session.rollback()  # 訊息寫入失敗不應阻斷報名

    flash('報名成功。系統訊息已發送至您的訊息匣。', 'success')
    return redirect(url_for('view_workshop', wid=wid))


@app.route('/workshops/<int:wid>/cancel', methods=['POST'])
@login_required
def cancel_workshop_registration(wid):
    if current_user.is_teacher:
        return redirect(url_for('dashboard'))
    p = WorkshopParticipation.query.filter_by(
        workshop_id=wid, user_id=current_user.id).first()
    if not p or not p.registered_at or p.cancelled_at:
        flash('您尚未報名或已取消。', 'error')
        return redirect(url_for('view_workshop', wid=wid))
    if p.checkin_at:
        flash('已簽到，無法取消報名。', 'error')
        return redirect(url_for('view_workshop', wid=wid))
    p.cancelled_at = _now()
    p.cancel_reason = request.form.get('cancel_reason', '').strip()
    db.session.commit()
    flash('已取消報名。', 'success')
    return redirect(url_for('view_workshop', wid=wid))


@app.route('/workshops/<int:wid>/checkin', methods=['GET', 'POST'])
@login_required
def checkin_workshop(wid):
    if current_user.is_teacher:
        return redirect(url_for('dashboard'))
    w = db.session.get(Workshop, wid)
    if not w:
        flash('找不到此工作坊。', 'error')
        return redirect(url_for('workshop_list'))
    p = WorkshopParticipation.query.filter_by(
        workshop_id=wid, user_id=current_user.id).first()

    if request.method == 'GET':
        return render_template('student/workshop_checkin.html', w=w, p=p)

    # POST
    code = (request.form.get('code', '') or '').strip()
    allowed, reason = _can_checkin(w, p)
    if not allowed:
        flash(reason, 'error')
        return redirect(url_for('checkin_workshop', wid=wid))
    if code != w.checkin_code:
        flash('簽到碼錯誤，請向現場教師確認。', 'error')
        return redirect(url_for('checkin_workshop', wid=wid))
    p.checkin_at = _now()
    p.checkin_method = 'self_code'
    db.session.commit()
    flash('簽到成功！活動結束後請記得填寫反思。', 'success')
    return redirect(url_for('view_workshop', wid=wid))


@app.route('/workshops/<int:wid>/reflection', methods=['GET', 'POST'])
@login_required
def submit_reflection(wid):
    if current_user.is_teacher:
        return redirect(url_for('dashboard'))
    w = db.session.get(Workshop, wid)
    if not w:
        flash('找不到此工作坊。', 'error')
        return redirect(url_for('workshop_list'))
    p = WorkshopParticipation.query.filter_by(
        workshop_id=wid, user_id=current_user.id).first()
    allowed, reason = _can_submit_reflection(w, p)
    if not allowed:
        flash(reason, 'error')
        return redirect(url_for('view_workshop', wid=wid))

    if request.method == 'GET':
        return render_template('student/workshop_reflection.html', w=w, p=p)

    # POST
    q1 = request.form.get('reflection_q1', '').strip()
    q2 = request.form.get('reflection_q2', '').strip()
    q3 = request.form.get('reflection_q3', '').strip()
    if min(len(q1), len(q2), len(q3)) < 30:
        flash('每題請至少填寫 30 字，描述具體一點才能反映你的學習。', 'error')
        # 暫存使用者輸入（不寫入 DB；簡化版直接要求重填）
        return render_template('student/workshop_reflection.html',
                               w=w, p=p,
                               draft={'q1': q1, 'q2': q2, 'q3': q3})
    p.reflection_q1 = q1
    p.reflection_q2 = q2
    p.reflection_q3 = q3
    p.reflection_submitted_at = _now()
    db.session.commit()
    flash('反思已送出。感謝你的當責記錄。', 'success')
    return redirect(url_for('view_workshop', wid=wid))


@app.route('/profile/workshops')
@login_required
def my_workshops():
    if current_user.is_teacher:
        return redirect(url_for('teacher_workshop_list'))
    parts = WorkshopParticipation.query.filter_by(
        user_id=current_user.id).all()
    parts.sort(key=lambda p: (p.workshop.starts_at if p.workshop else datetime.min),
               reverse=True)
    now = _now()
    items = []
    for p in parts:
        items.append({
            'participation': p,
            'workshop':      p.workshop,
            'status':        _workshop_status_for(p, p.workshop, now),
        })
    return render_template('student/my_workshops.html',
                           items=items, now=now,
                           type_labels=WORKSHOP_TYPE_LABELS)


# ─── Teacher: Oral Presentation Assessment ───────────────────────────────────

@app.route('/teacher/oral-assessment')
@login_required
def teacher_oral_assessment_list():
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    students = User.query.filter(
        User.experimental_group.isnot(None),
        User.experimental_group != ''
    ).order_by(User.experimental_group, User.class_group, User.name).all()
    assessments = {
        a.user_id: a
        for a in OralPresentationAssessment.query.filter_by(semester=SEMESTER).all()
    }
    return render_template('teacher/oral_assessment_list.html',
                           students=students, assessments=assessments)


@app.route('/teacher/oral-assessment/batch-open', methods=['POST'])
@login_required
def teacher_oral_assessment_batch_open():
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    students = User.query.filter(
        User.experimental_group.isnot(None),
        User.experimental_group != ''
    ).order_by(User.experimental_group, User.class_group, User.name).all()
    finalized_ids = {
        a.user_id for a in OralPresentationAssessment.query.filter_by(semester=SEMESTER)
        .filter(OralPresentationAssessment.finalized_at.isnot(None)).all()
    }
    for s in students:
        if s.id not in finalized_ids:
            return redirect(url_for('teacher_oral_assessment_detail', user_id=s.id))
    flash('所有學生已完成口頭報告評分。', 'success')
    return redirect(url_for('teacher_oral_assessment_list'))


@app.route('/teacher/oral-assessment/<int:user_id>', methods=['GET', 'POST'])
@login_required
def teacher_oral_assessment_detail(user_id):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    student = User.query.get_or_404(user_id)
    assessment = OralPresentationAssessment.query.filter_by(
        user_id=user_id, semester=SEMESTER
    ).first()

    if request.method == 'POST':
        # CSRF
        sent = request.form.get('_csrf') or request.headers.get('X-CSRFToken', '')
        expected = session.get('_csrf_token', '')
        if not expected or not secrets.compare_digest(sent, expected):
            abort(400, 'CSRF token invalid or missing.')

        # 已 finalized 不可再改
        if assessment and assessment.finalized_at:
            flash('此口頭報告評分已鎖定，不可再修改。', 'error')
            return redirect(url_for('teacher_oral_assessment_list'))

        def _parse_score(name):
            raw = request.form.get(name, '').strip()
            if not raw:
                return None, True
            try:
                v = int(raw)
            except ValueError:
                return None, False
            if not (1 <= v <= 5):
                return None, False
            return v, True

        sc, ok1 = _parse_score('score_content')
        ss, ok2 = _parse_score('score_structure')
        sd, ok3 = _parse_score('score_delivery')
        sq, ok4 = _parse_score('score_qa')
        if not all([ok1, ok2, ok3, ok4]):
            flash('分數格式錯誤，每項需為 1–5 整數。', 'error')
            return redirect(url_for('teacher_oral_assessment_detail', user_id=user_id))

        comment  = request.form.get('teacher_comment', '').strip()
        finalize = request.form.get('finalize') == '1'

        if not assessment:
            assessment = OralPresentationAssessment(user_id=user_id, semester=SEMESTER)
            db.session.add(assessment)

        assessment.score_content   = sc
        assessment.score_structure = ss
        assessment.score_delivery  = sd
        assessment.score_qa        = sq
        assessment.teacher_comment = comment
        assessment.updated_at      = datetime.utcnow()

        if finalize and all(s is not None for s in [sc, ss, sd, sq]):
            assessment.finalized_at = datetime.utcnow()
            assessment.reviewer_id  = current_user.id
            db.session.commit()
            flash(f'{student.name} 的口頭報告評分已確認。', 'success')
            return redirect(url_for('teacher_oral_assessment_list'))

        # 暫存（含 finalize 漏填情境，依然 commit 不丟資料）
        db.session.commit()
        if finalize:
            flash('請填寫全部 4 個評分向度後再確認；目前已暫存填寫部分。', 'error')
        else:
            flash('評分已暫存。', 'success')
        return redirect(url_for('teacher_oral_assessment_detail', user_id=user_id))

    # 取學生 journal 5 DP5 自評（perception series 參考）
    journal5 = LearningJournal.query.filter_by(
        user_id=user_id, journal_number=5, semester=SEMESTER
    ).first()
    dp5_perception = {}
    if journal5 and journal5.evaluation_json:
        try:
            dp5_perception = json.loads(journal5.evaluation_json).get('DP5', {})
        except Exception:
            pass

    return render_template('teacher/oral_assessment_detail.html',
                           student=student,
                           assessment=assessment,
                           dp5_perception=dp5_perception)


# ─── DB Init ──────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    _run_migrations()
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port,
            debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true')
