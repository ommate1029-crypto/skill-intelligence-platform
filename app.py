"""
AI-Enabled Skill Intelligence & Learning Platform
==================================================
MoSPI / DIID — Capacity building for India's Official Statistical System.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000

Optional: set ANTHROPIC_API_KEY as an environment variable to enable
LLM-generated MCQs (the app otherwise uses a built-in offline generator).
"""
import json
import os

from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from werkzeug.utils import secure_filename

from modules.models import db, User, Competency, Enrollment, Quiz, Question, QuizAttempt
from modules.competency_engine import compute_gaps, competency_summary, get_roles, get_all_skills
from modules.recommender import recommend_courses
from modules.quiz_generator import extract_text, generate_quiz

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXTENSIONS = {"txt", "pdf", "docx"}

app = Flask(__name__)
app.config["SECRET_KEY"] = "dev-secret-key-change-in-production"
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'platform.db')}"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # 15 MB

db.init_app(app)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# DB bootstrap + seed data
# ---------------------------------------------------------------------------

def seed_data():
    if User.query.first():
        return  # already seeded

    seed_users = [
        {"name": "Ananya Sharma", "designation": "Statistical Officer", "department": "NSSO",
         "role_key": "Statistical Officer", "experience_years": 4,
         "skills": {"Survey Design": 3, "Sampling": 3, "National Accounts": 1, "Python": 2,
                    "SQL": 1, "Data Privacy": 2, "Communication": 4, "Ethics": 4}},
        {"name": "Rahul Verma", "designation": "Data Analyst", "department": "DIID",
         "role_key": "Data Scientist / Analyst", "experience_years": 2,
         "skills": {"Python": 4, "R": 2, "SQL": 3, "AI/ML": 2, "Cloud Computing": 1,
                    "Data Visualization": 3, "APIs": 2}},
        {"name": "Priya Nair", "designation": "Survey Analyst", "department": "FOD",
         "role_key": "Survey Analyst", "experience_years": 6,
         "skills": {"Survey Design": 4, "Sampling": 4, "Agricultural Statistics": 2,
                    "Stata": 2, "SPSS": 1, "Communication": 3}},
        {"name": "Karan Mehta", "designation": "IT Officer", "department": "DIID",
         "role_key": "IT / Digital Governance Officer", "experience_years": 3,
         "skills": {"Cybersecurity": 2, "Data Privacy": 3, "Cloud Computing": 3, "APIs": 2}},
        {"name": "Dr. Sunita Rao", "designation": "Director", "department": "MoSPI HQ",
         "role_key": "Senior Administrator", "experience_years": 15, "is_admin": True,
         "skills": {"Leadership": 4, "Communication": 4, "Project Management": 3,
                    "Ethics": 4, "Decision Making": 3, "Change Management": 2}},
    ]

    for u in seed_users:
        user = User(
            name=u["name"], designation=u["designation"], department=u["department"],
            role_key=u["role_key"], experience_years=u["experience_years"],
            is_admin=u.get("is_admin", False),
        )
        db.session.add(user)
        db.session.flush()  # get user.id
        for skill, level in u["skills"].items():
            db.session.add(Competency(user_id=user.id, skill=skill, level=level))

    db.session.commit()


with app.app_context():
    db.create_all()
    seed_data()


# ---------------------------------------------------------------------------
# Auth-lite: simple user switcher (no password — prototype only)
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    existing_user = current_user()
    if existing_user:
        return redirect(url_for("admin_dashboard" if existing_user.is_admin else "employee_dashboard"))
    users = User.query.order_by(User.name).all()
    return render_template("login.html", users=users)


@app.route("/login/<int:user_id>")
def login(user_id):
    user = User.query.get_or_404(user_id)
    session["user_id"] = user.id
    if user.is_admin:
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("employee_dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


def current_user():
    uid = session.get("user_id")
    return User.query.get(uid) if uid else None


@app.context_processor
def inject_current_user():
    # Makes `user` available in every template automatically, even on pages
    # (like the login screen) that don't explicitly pass it in — avoids
    # jinja2.exceptions.UndefinedError when a stale session cookie is present.
    return {"user": current_user()}


# ---------------------------------------------------------------------------
# Employee: dashboard, profile, gap analysis, recommendations
# ---------------------------------------------------------------------------

@app.route("/dashboard")
def employee_dashboard():
    user = current_user()
    if not user:
        return redirect(url_for("index"))

    current_levels = {c.skill: c.level for c in user.competencies}
    summary = competency_summary(user.role_key, current_levels)
    recommendations = recommend_courses(summary["gaps"], top_n=6)
    enrollments = Enrollment.query.filter_by(user_id=user.id).all()
    quiz_attempts = QuizAttempt.query.filter_by(user_id=user.id).order_by(QuizAttempt.taken_at.desc()).limit(5).all()

    return render_template(
        "employee_dashboard.html", user=user, summary=summary,
        recommendations=recommendations, enrollments=enrollments,
        quiz_attempts=quiz_attempts,
    )


@app.route("/profile", methods=["GET", "POST"])
def profile():
    user = current_user()
    if not user:
        return redirect(url_for("index"))

    if request.method == "POST":
        user.designation = request.form.get("designation", user.designation)
        user.department = request.form.get("department", user.department)
        user.role_key = request.form.get("role_key", user.role_key)
        user.experience_years = int(request.form.get("experience_years") or 0)

        # Update / insert competency levels from submitted form
        for skill in get_all_skills():
            field = f"skill_{skill.replace(' ', '_').replace('/', '_')}"
            if field in request.form:
                level = int(request.form.get(field) or 0)
                comp = Competency.query.filter_by(user_id=user.id, skill=skill).first()
                if comp:
                    comp.level = level
                else:
                    db.session.add(Competency(user_id=user.id, skill=skill, level=level))
        db.session.commit()
        flash("Profile & competencies updated.", "success")
        return redirect(url_for("employee_dashboard"))

    current_levels = {c.skill: c.level for c in user.competencies}
    return render_template(
        "profile.html", user=user, roles=get_roles(),
        all_skills=get_all_skills(), current_levels=current_levels,
    )


@app.route("/recommendations/enroll/<course_id>")
def enroll_course(course_id):
    user = current_user()
    if not user:
        return redirect(url_for("index"))

    courses = json.load(open(os.path.join(BASE_DIR, "data", "igot_courses.json")))
    course = next((c for c in courses if c["id"] == course_id), None)
    if course:
        existing = Enrollment.query.filter_by(user_id=user.id, course_id=course_id).first()
        if not existing:
            db.session.add(Enrollment(
                user_id=user.id, course_id=course_id, course_title=course["title"],
                status="Enrolled", progress_pct=0,
            ))
            db.session.commit()
            flash(f"Enrolled in '{course['title']}' via iGOT Karmayogi.", "success")
    return redirect(url_for("employee_dashboard"))


# ---------------------------------------------------------------------------
# Intelligent Assessment Engine: quiz / MCQ generation from uploaded material
# ---------------------------------------------------------------------------

@app.route("/quiz/generate", methods=["GET", "POST"])
def quiz_generate():
    user = current_user()
    if not user:
        return redirect(url_for("index"))

    if request.method == "POST":
        file = request.files.get("material")
        num_questions = int(request.form.get("num_questions") or 5)
        quiz_title = request.form.get("quiz_title") or "Untitled Quiz"

        if not file or file.filename == "" or not allowed_file(file.filename):
            flash("Please upload a .txt, .pdf, or .docx file.", "danger")
            return redirect(url_for("quiz_generate"))

        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)

        try:
            text = extract_text(filepath)
            if len(text.strip()) < 100:
                flash("Could not extract enough text from this file to build a quiz.", "warning")
                return redirect(url_for("quiz_generate"))

            questions, method = generate_quiz(text, num_questions=num_questions)
            if not questions:
                flash("No questions could be generated from this material. Try a longer document.", "warning")
                return redirect(url_for("quiz_generate"))

            quiz = Quiz(title=quiz_title, source_filename=filename,
                        created_by=user.id, generation_method=method)
            db.session.add(quiz)
            db.session.flush()

            for q in questions:
                opts = q["options"]
                db.session.add(Question(
                    quiz_id=quiz.id, text=q["question"],
                    option_a=opts.get("A"), option_b=opts.get("B"),
                    option_c=opts.get("C"), option_d=opts.get("D"),
                    correct_option=q["correct_option"],
                    explanation=q.get("explanation", ""),
                ))
            db.session.commit()
            flash(f"Quiz generated using the {method} engine ({len(questions)} questions).", "success")
            return redirect(url_for("quiz_take", quiz_id=quiz.id))

        except Exception as exc:
            flash(f"Error generating quiz: {exc}", "danger")
            return redirect(url_for("quiz_generate"))

    recent_quizzes = Quiz.query.order_by(Quiz.created_at.desc()).limit(10).all()
    return render_template("quiz_generator.html", user=user, recent_quizzes=recent_quizzes)


@app.route("/quiz/<int:quiz_id>")
def quiz_take(quiz_id):
    user = current_user()
    if not user:
        return redirect(url_for("index"))
    quiz = Quiz.query.get_or_404(quiz_id)
    return render_template("quiz_take.html", user=user, quiz=quiz)


@app.route("/quiz/<int:quiz_id>/submit", methods=["POST"])
def quiz_submit(quiz_id):
    user = current_user()
    if not user:
        return redirect(url_for("index"))
    quiz = Quiz.query.get_or_404(quiz_id)

    score = 0
    results = []
    for q in quiz.questions:
        chosen = request.form.get(f"q_{q.id}")
        is_correct = (chosen == q.correct_option)
        if is_correct:
            score += 1
        results.append({
            "question": q, "chosen": chosen, "is_correct": is_correct,
        })

    attempt = QuizAttempt(quiz_id=quiz.id, user_id=user.id, score=score, total=len(quiz.questions))
    db.session.add(attempt)
    db.session.commit()

    return render_template("quiz_result.html", user=user, quiz=quiz, results=results,
                            score=score, total=len(quiz.questions))


# ---------------------------------------------------------------------------
# Admin: organization-wide analytics dashboard
# ---------------------------------------------------------------------------

@app.route("/admin")
def admin_dashboard():
    user = current_user()
    if not user or not user.is_admin:
        return redirect(url_for("index"))

    all_users = User.query.filter_by(is_admin=False).all()

    org_domain_totals = {}
    readiness_by_user = []
    skill_gap_counter = {}

    for u in all_users:
        current_levels = {c.skill: c.level for c in u.competencies}
        summary = competency_summary(u.role_key, current_levels)
        readiness_by_user.append({
            "name": u.name, "role": u.role_key, "readiness": summary["overall_readiness"],
            "critical_gaps": len(summary["critical_gaps"]),
        })
        for domain, score in summary["domain_readiness"].items():
            org_domain_totals.setdefault(domain, []).append(score)
        for g in summary["gaps"]:
            if g["gap"] > 0:
                skill_gap_counter[g["skill"]] = skill_gap_counter.get(g["skill"], 0) + g["gap"]

    org_domain_avg = {
        d: round(sum(scores) / len(scores)) for d, scores in org_domain_totals.items()
    }
    top_skill_gaps = sorted(skill_gap_counter.items(), key=lambda x: x[1], reverse=True)[:8]

    total_enrollments = Enrollment.query.count()
    completed = Enrollment.query.filter_by(status="Completed").count()
    total_quizzes = Quiz.query.count()
    total_attempts = QuizAttempt.query.count()
    avg_quiz_score_pct = 0
    attempts = QuizAttempt.query.all()
    if attempts:
        avg_quiz_score_pct = round(
            sum(a.score / a.total for a in attempts if a.total) / len(attempts) * 100
        )

    return render_template(
        "admin_dashboard.html", user=user, readiness_by_user=readiness_by_user,
        org_domain_avg=org_domain_avg, top_skill_gaps=top_skill_gaps,
        total_users=len(all_users), total_enrollments=total_enrollments,
        completed=completed, total_quizzes=total_quizzes,
        total_attempts=total_attempts, avg_quiz_score_pct=avg_quiz_score_pct,
    )


# ---------------------------------------------------------------------------
# JSON API (for charts / potential external integration)
# ---------------------------------------------------------------------------

@app.route("/api/gap-analysis/<int:user_id>")
def api_gap_analysis(user_id):
    user = User.query.get_or_404(user_id)
    current_levels = {c.skill: c.level for c in user.competencies}
    return jsonify(competency_summary(user.role_key, current_levels))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
