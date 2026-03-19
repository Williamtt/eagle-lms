import os
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename
from config import Config
from models import db, User, Submission, AIFeedback, TeacherReview
import ai_service

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = '請先登入。'

ALLOWED_EXTENSIONS = {'pdf', 'xlsx', 'xls', 'docx', 'doc', 'png', 'jpg', 'jpeg', 'zip'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ─── Auth Routes ───

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        student_id = request.form.get('student_id', '').strip()
        name = request.form.get('name', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        class_group = request.form.get('class_group', '').strip() or '未分班'
        teacher_code = request.form.get('teacher_code', '').strip()

        if not student_id or not name or not password:
            flash('請填寫所有必填欄位。', 'error')
            return render_template('register.html')

        if password != confirm:
            flash('兩次密碼不一致。', 'error')
            return render_template('register.html')

        if len(password) < 6:
            flash('密碼至少需要 6 個字元。', 'error')
            return render_template('register.html')

        if User.query.filter_by(student_id=student_id).first():
            flash('此學號已被註冊。', 'error')
            return render_template('register.html')

        role = 'teacher' if teacher_code == app.config['TEACHER_CODE'] else 'student'
        user = User(student_id=student_id, name=name, role=role, class_group=class_group)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash('註冊成功！請登入。', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        student_id = request.form.get('student_id', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(student_id=student_id).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('學號或密碼錯誤。', 'error')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


# ─── Main Routes ───

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/manual')
def manual():
    return render_template('manual.html')


@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.is_teacher:
        return redirect(url_for('teacher_dashboard'))
    # Student dashboard
    submissions = Submission.query.filter_by(user_id=current_user.id).order_by(Submission.submitted_at.desc()).all()
    task_status = {}
    for t in range(1, 5):
        task_subs = [s for s in submissions if s.task_number == t]
        reviewed_count = 0
        for s in task_subs:
            tr = s.teacher_reviews.first()
            if tr and tr.published:
                reviewed_count += 1
        task_status[t] = {
            'submissions': len(task_subs),
            'reviewed': reviewed_count,
            'has_ai_feedback': any(s.ai_feedbacks.first() for s in task_subs)
        }
    return render_template('student/dashboard.html', task_status=task_status, submissions=submissions)


# ─── Student: Task & Submission ───

@app.route('/task/<int:task_number>')
@login_required
def view_task(task_number):
    if task_number < 1 or task_number > 4:
        flash('無效的任務編號。', 'error')
        return redirect(url_for('dashboard'))
    submissions = Submission.query.filter_by(
        user_id=current_user.id, task_number=task_number
    ).order_by(Submission.submitted_at.desc()).all()
    return render_template('student/task.html', task_number=task_number, submissions=submissions)


@app.route('/submit/<int:task_number>', methods=['POST'])
@login_required
def submit_task(task_number):
    submission_type = request.form.get('submission_type', 'reflection')
    content = request.form.get('content', '').strip()

    # Handle checklist data
    checklist_data = ''
    if submission_type in ('checklist', 'self_assessment'):
        checklist_items = {}
        for key, val in request.form.items():
            if key.startswith('checklist_') or key.startswith('assessment_'):
                checklist_items[key] = val
        checklist_data = json.dumps(checklist_items, ensure_ascii=False)

    # Handle file upload
    file_path = ''
    file_name = ''
    file = request.files.get('file')
    if file and file.filename and allowed_file(file.filename):
        file_name = secure_filename(f"{current_user.student_id}_t{task_number}_{file.filename}")
        upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], str(current_user.id))
        os.makedirs(upload_dir, exist_ok=True)
        fpath = os.path.join(upload_dir, file_name)
        file.save(fpath)
        file_path = fpath
        file_name = file.filename

    if not content and not file_path and not checklist_data:
        flash('請至少填寫文字內容或上傳檔案。', 'error')
        return redirect(url_for('view_task', task_number=task_number))

    submission = Submission(
        user_id=current_user.id,
        task_number=task_number,
        submission_type=submission_type,
        content=content,
        checklist_data=checklist_data,
        file_path=file_path,
        file_name=file_name
    )
    db.session.add(submission)
    db.session.commit()

    # Generate AI instant feedback if content exists
    if content and app.config.get('ANTHROPIC_API_KEY'):
        result = ai_service.generate_instant_feedback(
            task_number, submission_type, content, current_user.name
        )
        ai_fb = AIFeedback(
            submission_id=submission.id,
            feedback=result.get('feedback', ''),
            scores=json.dumps(result.get('scores', {}), ensure_ascii=False),
            model_used='claude-sonnet-4-20250514'
        )
        db.session.add(ai_fb)
        db.session.commit()
        flash('提交成功！AI 助教已提供初步回饋。', 'success')
    else:
        flash('提交成功！', 'success')

    return redirect(url_for('view_submission', submission_id=submission.id))


@app.route('/submission/<int:submission_id>')
@login_required
def view_submission(submission_id):
    submission = db.session.get(Submission, submission_id)
    if not submission:
        flash('找不到此提交。', 'error')
        return redirect(url_for('dashboard'))
    # Students can only see their own; teachers can see all
    if not current_user.is_teacher and submission.user_id != current_user.id:
        flash('無權限查看。', 'error')
        return redirect(url_for('dashboard'))

    ai_fb = submission.ai_feedbacks.first()
    teacher_review = submission.teacher_reviews.first()
    # Students only see published reviews
    if not current_user.is_teacher and teacher_review and not teacher_review.published:
        teacher_review = None

    return render_template('student/submission_detail.html',
                           submission=submission, ai_feedback=ai_fb,
                           teacher_review=teacher_review)


# ─── Teacher Routes ───

@app.route('/teacher')
@login_required
def teacher_dashboard():
    if not current_user.is_teacher:
        flash('無教師權限。', 'error')
        return redirect(url_for('dashboard'))

    students = User.query.filter_by(role='student').order_by(User.class_group, User.student_id).all()
    total_submissions = Submission.query.count()
    reviewed_count = TeacherReview.query.filter_by(published=True).count()

    task_stats = {}
    for t in range(1, 5):
        subs = Submission.query.filter_by(task_number=t).all()
        unique_students = len(set(s.user_id for s in subs))
        task_stats[t] = {
            'total_submissions': len(subs),
            'unique_students': unique_students,
            'reviewed': sum(1 for s in subs if s.teacher_reviews.first())
        }

    return render_template('teacher/dashboard.html',
                           students=students, total_submissions=total_submissions,
                           reviewed_count=reviewed_count, task_stats=task_stats)


@app.route('/teacher/task/<int:task_number>')
@login_required
def teacher_task_submissions(task_number):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))
    submissions = Submission.query.filter_by(task_number=task_number)\
        .order_by(Submission.submitted_at.desc()).all()
    return render_template('teacher/submissions.html',
                           task_number=task_number, submissions=submissions)


@app.route('/teacher/review/<int:submission_id>', methods=['GET', 'POST'])
@login_required
def teacher_review(submission_id):
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))

    submission = db.session.get(Submission, submission_id)
    if not submission:
        flash('找不到此提交。', 'error')
        return redirect(url_for('teacher_dashboard'))

    if request.method == 'POST':
        feedback = request.form.get('feedback', '').strip()
        score = request.form.get('score', None)
        publish = request.form.get('publish') == 'on'

        existing = submission.teacher_reviews.first()
        if existing:
            existing.feedback = feedback
            existing.score = float(score) if score else None
            existing.published = publish
            existing.reviewed_at = datetime.utcnow()
        else:
            review = TeacherReview(
                submission_id=submission.id,
                teacher_id=current_user.id,
                feedback=feedback,
                score=float(score) if score else None,
                published=publish
            )
            db.session.add(review)
        db.session.commit()
        flash('評閱已儲存。' + (' 已發布給學生。' if publish else ''), 'success')
        return redirect(url_for('teacher_task_submissions', task_number=submission.task_number))

    ai_fb = submission.ai_feedbacks.first()
    existing_review = submission.teacher_reviews.first()

    # Get AI suggestion for teacher
    ai_suggestion = None
    if submission.content and app.config.get('ANTHROPIC_API_KEY'):
        ai_suggestion = ai_service.generate_review_suggestion(
            submission.content, submission.task_number, submission.submission_type
        )

    return render_template('teacher/review.html',
                           submission=submission, ai_feedback=ai_fb,
                           existing_review=existing_review, ai_suggestion=ai_suggestion)


@app.route('/teacher/analytics')
@login_required
def teacher_analytics():
    if not current_user.is_teacher:
        return redirect(url_for('dashboard'))

    task_number = request.args.get('task', 1, type=int)
    submissions = Submission.query.filter_by(task_number=task_number).all()

    # Prepare data for AI analysis
    submissions_data = []
    for s in submissions:
        ai_fb = s.ai_feedbacks.first()
        submissions_data.append({
            "student_id": s.author.student_id,
            "class": s.author.class_group,
            "type": s.submission_type,
            "content_preview": s.content[:500] if s.content else "",
            "ai_scores": json.loads(ai_fb.scores) if ai_fb and ai_fb.scores else {}
        })

    analysis = ""
    if submissions_data and app.config.get('ANTHROPIC_API_KEY'):
        analysis = ai_service.generate_teacher_analysis(submissions_data)

    return render_template('teacher/analytics.html',
                           task_number=task_number, submissions=submissions,
                           analysis=analysis, submissions_data=submissions_data)


# ─── API for AI feedback (re-generate) ───

@app.route('/api/regenerate-feedback/<int:submission_id>', methods=['POST'])
@login_required
def regenerate_feedback(submission_id):
    submission = db.session.get(Submission, submission_id)
    if not submission or not submission.content:
        return jsonify({"error": "無法重新生成"}), 400

    result = ai_service.generate_instant_feedback(
        submission.task_number, submission.submission_type,
        submission.content, submission.author.name
    )
    ai_fb = AIFeedback(
        submission_id=submission.id,
        feedback=result.get('feedback', ''),
        scores=json.dumps(result.get('scores', {}), ensure_ascii=False)
    )
    db.session.add(ai_fb)
    db.session.commit()
    return jsonify({"success": True, "feedback": result.get('feedback', '')})


# ─── File serving ───

@app.route('/uploads/<path:filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ─── DB init ───

with app.app_context():
    db.create_all()
    # Ensure upload dir exists
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true')
