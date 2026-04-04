import os
import csv
import io
import json
from datetime import datetime
from flask import (Flask, render_template, request, redirect,
                   url_for, flash, send_from_directory, jsonify,
                   Response)
from flask_login import (LoginManager, login_user, logout_user,
                         login_required, current_user)
from werkzeug.utils import secure_filename

from config import Config
from models import (db, User,
                    TaskSubmission, QuestionResponse, ChecklistResponse,
                    ReflectionResponse, DeliverableUpload,
                    AIFeedback, TeacherReview,
                    Questionnaire, QuestionnaireItem,
                    QuestionnaireSubmission, QuestionnaireAnswer,
                    LearningJournal,
                    Submission,          # Submission 保留供舊資料查詢
                    TutorConversation)
import requests as http_requests
import ai_service
import notify
from task_definitions import TASKS, SEMESTER, SYSTEM_VERSION, LEARNING_JOURNALS

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
        if 'users' in tables:
            cols = [c['name'] for c in inspector.get_columns('users')]
            if 'status' not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text(
                        "ALTER TABLE users ADD COLUMN status VARCHAR(10) DEFAULT 'active'"
                    ))
                    conn.commit()


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = '請先登入。'

ALLOWED_EXTENSIONS = {'pdf', 'xlsx', 'xls', 'docx', 'doc',
                      'png', 'jpg', 'jpeg', 'zip'}

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

        # 必填欄位
        if not student_id or not name or not password:
            flash('請填寫所有必填欄位。', 'error')
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
                    role=role, class_group=cg, status=status)
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


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


# ─── Static Pages ─────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/manual')
def manual():
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
        journal = LearningJournal.query.filter_by(
            user_id=current_user.id,
            journal_number=j_num,
            semester=SEMESTER
        ).first()
        journal_status[j_num] = {
            'title':      j_def['title'],
            'week':       j_def['week'],
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

    return render_template('student/dashboard.html',
                           task_status=task_status,
                           journal_status=journal_status,
                           active_questionnaires=active_questionnaires,
                           completed_q_codes=completed_q_codes,
                           tasks=TASKS)


# ─── Student: Task & Structured Submission ────────────────────────────────────

@app.route('/task/<int:task_number>')
@login_required
def view_task(task_number):
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
        db.session.add(AIFeedback(
            task_submission_id = sub.id,
            feedback_type      = 'overall',
            feedback           = result.get('feedback', ''),
            scores             = json.dumps(
                result.get('scores', {}), ensure_ascii=False),
            model_used         = 'claude-sonnet-4-20250514'
        ))
        db.session.commit()
        flash('提交成功！AI 助教已提供初步回饋。', 'success')
    else:
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


# ─── Student: Learning Journal ────────────────────────────────────────────────

@app.route('/journal')
@login_required
def journal_list():
    journals = {j['journal_number']: j for j in LEARNING_JOURNALS}
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
    j_defs = {j['journal_number']: j for j in LEARNING_JOURNALS}
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
            if existing:
                existing.content    = content
                existing.updated_at = datetime.utcnow()
            else:
                existing = LearningJournal(
                    user_id        = current_user.id,
                    journal_number = journal_number,
                    week           = j_def['week'],
                    semester       = SEMESTER,
                    content        = content,
                )
                db.session.add(existing)
            db.session.commit()
            flash('學習日誌已儲存。', 'success')
            return redirect(url_for('view_journal', journal_number=journal_number))

    return render_template('student/journal.html',
                           j_def=j_def,
                           existing=existing)


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
    total_subs    = TaskSubmission.query.filter(
        TaskSubmission.semester == SEMESTER,
        TaskSubmission.status != 'draft'
    ).count()
    reviewed_count = TeacherReview.query.filter_by(published=True).count()

    task_stats = {}
    for t_num, t_def in TASKS.items():
        subs = TaskSubmission.query.filter(
            TaskSubmission.task_number == t_num,
            TaskSubmission.semester == SEMESTER,
            TaskSubmission.status != 'draft'
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

    return render_template('teacher/dashboard.html',
                           students=students,
                           pending_users=pending_users,
                           disabled_users=disabled_users,
                           total_submissions=total_subs,
                           reviewed_count=reviewed_count,
                           task_stats=task_stats,
                           tasks=TASKS)


@app.route('/teacher/user/<int:uid>/approve', methods=['POST'])
@login_required
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
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))

    sub = db.session.get(TaskSubmission, submission_id)
    if not sub:
        flash('找不到此提交。', 'error')
        return redirect(url_for('teacher_dashboard'))

    task_def = TASKS.get(sub.task_number, {})

    if request.method == 'POST':
        feedback = request.form.get('feedback', '').strip()
        score    = request.form.get('score', None)
        publish  = request.form.get('publish') == 'on'

        existing_review = sub.teacher_reviews.first()
        if existing_review:
            existing_review.feedback    = feedback
            existing_review.score       = float(score) if score else None
            existing_review.published   = publish
            existing_review.reviewed_at = datetime.utcnow()
        else:
            db.session.add(TeacherReview(
                task_submission_id = sub.id,
                teacher_id         = current_user.id,
                feedback           = feedback,
                score              = float(score) if score else None,
                published          = publish,
            ))
        db.session.commit()
        flash('評閱已儲存。' + (' 已發布給學生。' if publish else ''), 'success')
        return redirect(url_for('teacher_task_submissions',
                                task_number=sub.task_number))

    # 整理回答供顯示
    pq_map = {r.question_id: r.answer for r in sub.question_responses}
    cl_map = {r.item_id: r            for r in sub.checklist_responses}
    rq_map = {r.question_id: r.answer for r in sub.reflection_responses}
    du_map = {du.deliverable_id: du   for du in sub.deliverable_uploads}

    ai_fb          = sub.ai_feedbacks.order_by(AIFeedback.created_at.desc()).first()
    existing_review = sub.teacher_reviews.first()

    ai_suggestion = None
    if sub and app.config.get('ANTHROPIC_API_KEY'):
        submission_text = _build_submission_text_for_ai(sub, task_def)
        if submission_text.strip():
            ai_suggestion = ai_service.generate_review_suggestion(
                submission_text, sub.task_number, 'structured'
            )

    return render_template('teacher/review.html',
                           sub=sub,
                           task_def=task_def,
                           pq_map=pq_map,
                           cl_map=cl_map,
                           rq_map=rq_map,
                           du_map=du_map,
                           ai_feedback=ai_fb,
                           existing_review=existing_review,
                           ai_suggestion=ai_suggestion)


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

@app.route('/api/regenerate-feedback/<int:submission_id>', methods=['POST'])
@login_required
def regenerate_feedback(submission_id):
    sub = db.session.get(TaskSubmission, submission_id)
    if not sub:
        return jsonify({'error': '找不到提交'}), 404
    if not current_user.is_teacher and sub.user_id != current_user.id:
        return jsonify({'error': '無權限'}), 403

    task_def = TASKS.get(sub.task_number, {})
    text     = _build_submission_text_for_ai(sub, task_def)
    result   = ai_service.generate_instant_feedback(
        sub.task_number, 'structured', text, sub.author.name
    )
    db.session.add(AIFeedback(
        task_submission_id = sub.id,
        feedback_type      = 'overall',
        feedback           = result.get('feedback', ''),
        scores             = json.dumps(result.get('scores', {}), ensure_ascii=False),
        model_used         = 'claude-sonnet-4-20250514'
    ))
    db.session.commit()
    return jsonify({'success': True, 'feedback': result.get('feedback', '')})


# ─── AI Tutor API ────────────────────────────────────────────────────────────

@app.route('/api/tutor/chat', methods=['POST'])
@login_required
def tutor_chat():
    data = request.get_json()
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

    messages = json.loads(conv.messages)

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
        result = resp.json()
    except Exception:
        return jsonify({'error': 'AI 服務連線失敗'}), 502

    # Append both messages to conversation
    messages.append({'role': 'user', 'content': user_message})
    messages.append({'role': 'assistant', 'content': result['answer']})
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


# ─── DB Init ──────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    _run_migrations()
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port,
            debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true')
