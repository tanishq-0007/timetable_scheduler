from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, UserMixin, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import random
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'replace_this_secret_key_!_change_in_production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///timetable.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ---------- MODELS ----------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(30), default='admin')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Teacher(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)

class Subject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    weekly_classes = db.Column(db.Integer, default=2)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teacher.id'), nullable=True)

class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    capacity = db.Column(db.Integer, default=40)

class Timetable(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.String(20))
    slot = db.Column(db.String(20))
    subject_id = db.Column(db.Integer, db.ForeignKey('subject.id'))
    teacher_id = db.Column(db.Integer, db.ForeignKey('teacher.id'))
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'))

# ---------- User loader ----------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ---------- Utilities ----------
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
SLOTS = ["9-10", "10-11", "11-12", "1-2", "2-3", "3-4"]

def clear_timetable():
    Timetable.query.delete()
    db.session.commit()

# Greedy scheduler with simple constraints:
def generate_greedy(max_classes_per_teacher_per_day=3):
    subjects = Subject.query.all()
    teachers = {t.id: t for t in Teacher.query.all()}
    rooms = Room.query.all()

    # Build demand list
    demand = []
    for s in subjects:
        for _ in range(max(0, s.weekly_classes or 0)):
            demand.append(s.id)

    random.shuffle(demand)

    clear_timetable()

    placed = []  # list of dicts: {day,slot,subject_id,teacher_id,room_id}
    teacher_day_load = {}  # teacher_id -> day -> count
    for t in teachers.values():
        teacher_day_load[t.id] = {d: 0 for d in DAYS}

    for subj_id in demand:
        subj = Subject.query.get(subj_id)
        teacher = teachers.get(subj.teacher_id) if subj.teacher_id else None
        # If no teacher assigned, pick any teacher (best-effort)
        if not teacher:
            all_teachers = list(teachers.values())
            teacher = random.choice(all_teachers) if all_teachers else None

        placed_flag = False
        for d in DAYS:
            if teacher and teacher_day_load[teacher.id][d] >= max_classes_per_teacher_per_day:
                continue
            for s in SLOTS:
                # check teacher clash
                if teacher and any(p['day'] == d and p['slot'] == s and p['teacher_id'] == teacher.id for p in placed):
                    continue
                # check room availability
                for r in rooms:
                    if any(p['day'] == d and p['slot'] == s and p['room_id'] == r.id for p in placed):
                        continue
                    # discourage direct repeat for same teacher & subject back-to-back
                    # (simple check previous slot)
                    slot_idx = SLOTS.index(s)
                    prev_slot = SLOTS[slot_idx - 1] if slot_idx > 0 else None
                    if prev_slot and teacher and any(p['day'] == d and p['slot'] == prev_slot and p['teacher_id'] == teacher.id and p['subject_id'] == subj.id for p in placed):
                        continue
                    # place
                    placed.append({'day': d, 'slot': s, 'subject_id': subj.id, 'teacher_id': teacher.id if teacher else None, 'room_id': r.id})
                    if teacher:
                        teacher_day_load[teacher.id][d] += 1
                    placed_flag = True
                    break
                if placed_flag:
                    break
            if placed_flag:
                break
        # if not placed, skip (could be improved by backtracking)
    # persist to DB
    for p in placed:
        new = Timetable(day=p['day'], slot=p['slot'], subject_id=p['subject_id'], teacher_id=p['teacher_id'], room_id=p['room_id'])
        db.session.add(new)
    db.session.commit()
    return len(placed)

# Simple heuristic optimizer (lightweight)
def optimize_heuristic():
    entries = Timetable.query.all()
    # convert to list for in-memory changes
    placed = []
    for e in entries:
        placed.append({'id': e.id, 'day': e.day, 'slot': e.slot, 'subject_id': e.subject_id, 'teacher_id': e.teacher_id, 'room_id': e.room_id})

    # Build teacher-day load
    teacher_day_load = {}
    teachers = Teacher.query.all()
    for t in teachers:
        teacher_day_load[t.id] = {d: 0 for d in DAYS}
    for p in placed:
        if p['teacher_id']:
            teacher_day_load[p['teacher_id']][p['day']] += 1

    # Try pairwise swaps of day/slot between entries for same teacher to reduce max daily load
    for i in range(len(placed)):
        for j in range(i + 1, len(placed)):
            a = placed[i]
            b = placed[j]
            if a['teacher_id'] != b['teacher_id']:
                continue
            if a['day'] == b['day']:
                continue
            t_id = a['teacher_id']
            before = max(teacher_day_load[t_id][a['day']], teacher_day_load[t_id][b['day']])
            # simulate swap
            teacher_day_load[t_id][a['day']] -= 1
            teacher_day_load[t_id][b['day']] -= 1
            teacher_day_load[t_id][a['day']] += 1
            teacher_day_load[t_id][b['day']] += 1
            after = max(teacher_day_load[t_id][a['day']], teacher_day_load[t_id][b['day']])
            # revert
            teacher_day_load[t_id][a['day']] -= 1
            teacher_day_load[t_id][b['day']] -= 1
            teacher_day_load[t_id][a['day']] += 1
            teacher_day_load[t_id][b['day']] += 1
            if after < before:
                # check clash free if swapped
                def clash_if_swap(ai, aj):
                    for k,other in enumerate(placed):
                        if k == ai or k == aj:
                            continue
                        # if swapping would cause teacher clash:
                        if other['teacher_id'] == placed[aj]['teacher_id'] and other['day'] == placed[ai]['day'] and other['slot'] == placed[ai]['slot']:
                            return True
                        if other['teacher_id'] == placed[ai]['teacher_id'] and other['day'] == placed[aj]['day'] and other['slot'] == placed[aj]['slot']:
                            return True
                        # room clash
                        if other['room_id'] == placed[aj]['room_id'] and other['day'] == placed[ai]['day'] and other['slot'] == placed[ai]['slot']:
                            return True
                        if other['room_id'] == placed[ai]['room_id'] and other['day'] == placed[aj]['day'] and other['slot'] == placed[aj]['slot']:
                            return True
                    return False
                if not clash_if_swap(i, j):
                    # swap day & slot & room
                    placed[i]['day'], placed[j]['day'] = placed[j]['day'], placed[i]['day']
                    placed[i]['slot'], placed[j]['slot'] = placed[j]['slot'], placed[i]['slot']
                    placed[i]['room_id'], placed[j]['room_id'] = placed[j]['room_id'], placed[i]['room_id']
    # persist updated placements
    for p in placed:
        e = Timetable.query.get(p['id'])
        if e:
            e.day = p['day']; e.slot = p['slot']; e.room_id = p['room_id']
    db.session.commit()
    return len(placed)

# ---------- ROUTES ----------
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        user = User.query.filter_by(username=u).first()
        if user and user.check_password(p):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid credentials', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

# Teachers
@app.route('/teachers', methods=['GET', 'POST'])
@login_required
def teachers():
    if request.method == 'POST':
        name = request.form.get('name')
        if name:
            db.session.add(Teacher(name=name.strip()))
            db.session.commit()
            flash('Teacher added', 'success')
            return redirect(url_for('teachers'))
    all_teachers = Teacher.query.all()
    return render_template('teachers.html', teachers=all_teachers)

# Subjects
@app.route('/subjects', methods=['GET', 'POST'])
@login_required
def subjects():
    teachers = Teacher.query.all()
    if request.method == 'POST':
        name = request.form.get('name')
        weekly = int(request.form.get('weekly_classes') or 0)
        teacher_id = request.form.get('teacher_id') or None
        if name:
            db.session.add(Subject(name=name.strip(), weekly_classes=weekly, teacher_id=int(teacher_id) if teacher_id else None))
            db.session.commit()
            flash('Subject added', 'success')
            return redirect(url_for('subjects'))
    all_subjects = Subject.query.all()
    return render_template('subjects.html', subjects=all_subjects, teachers=teachers)

# Rooms
@app.route('/rooms', methods=['GET', 'POST'])
@login_required
def rooms():
    if request.method == 'POST':
        name = request.form.get('name')
        cap = int(request.form.get('capacity') or 40)
        if name:
            db.session.add(Room(name=name.strip(), capacity=cap))
            db.session.commit()
            flash('Room added', 'success')
            return redirect(url_for('rooms'))
    all_rooms = Room.query.all()
    return render_template('rooms.html', rooms=all_rooms)

# Generate Timetable
@app.route('/generate', methods=['POST'])
@login_required
def generate():
    max_per_day = int(request.form.get('max_per_day') or 3)
    count = generate_greedy(max_classes_per_teacher_per_day=max_per_day)
    flash(f'Timetable generated: placed {count} sessions', 'success')
    return redirect(url_for('timetable_view'))

# AI Optimize
@app.route('/optimize', methods=['POST'])
@login_required
def optimize():
    count = optimize_heuristic()
    flash(f'AI optimization applied ({count} sessions processed)', 'info')
    return redirect(url_for('timetable_view'))

# Timetable view
@app.route('/timetable')
@login_required
def timetable_view():
    # get timetable entries and format into grid
    entries = Timetable.query.all()
    grid = {d: {s: None for s in SLOTS} for d in DAYS}
    for e in entries:
        subj = Subject.query.get(e.subject_id)
        teacher = Teacher.query.get(e.teacher_id)
        room = Room.query.get(e.room_id)
        grid[e.day][e.slot] = {'subject': subj.name if subj else 'N/A', 'teacher': teacher.name if teacher else 'N/A', 'room': room.name if room else 'N/A'}
    return render_template('timetable.html', grid=grid, days=DAYS, slots=SLOTS)

# API: simple endpoints (for possible React integration)
@app.route('/api/teachers', methods=['GET','POST'])
def api_teachers():
    if request.method == 'POST':
        data = request.json or {}
        name = data.get('name')
        if name:
            t = Teacher(name=name)
            db.session.add(t); db.session.commit()
            return jsonify({'id': t.id, 'name': t.name}), 201
    teachers = Teacher.query.all()
    return jsonify([{'id':t.id,'name':t.name} for t in teachers])

@app.route('/api/subjects', methods=['GET','POST'])
def api_subjects():
    if request.method == 'POST':
        data = request.json or {}
        s = Subject(name=data.get('name'), weekly_classes=int(data.get('weekly_classes',0)), teacher_id=data.get('teacher_id'))
        db.session.add(s); db.session.commit()
        return jsonify({'id': s.id, 'name': s.name}), 201
    subs = Subject.query.all()
    return jsonify([{'id':s.id,'name':s.name,'weekly_classes':s.weekly_classes,'teacher_id':s.teacher_id} for s in subs])

@app.route('/api/rooms', methods=['GET','POST'])
def api_rooms():
    if request.method == 'POST':
        data = request.json or {}
        r = Room(name=data.get('name'), capacity=int(data.get('capacity',40)))
        db.session.add(r); db.session.commit()
        return jsonify({'id': r.id, 'name': r.name}), 201
    rs = Room.query.all()
    return jsonify([{'id':r.id,'name':r.name,'capacity':r.capacity} for r in rs])

@app.route('/api/timetable', methods=['GET'])
def api_timetable():
    entries = Timetable.query.all()
    out = []
    for e in entries:
        subj = Subject.query.get(e.subject_id)
        teacher = Teacher.query.get(e.teacher_id)
        room = Room.query.get(e.room_id)
        out.append({'day': e.day, 'slot': e.slot, 'subject': subj.name if subj else None, 'teacher': teacher.name if teacher else None, 'room': room.name if room else None})
    return jsonify(out)

# ---------- Main ----------
if __name__ == '__main__':
    with app.app_context():
        db_exists = os.path.exists('timetable.db')
        db.create_all()
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin')
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
        if not db_exists:
            # seed small sample
            t1 = Teacher(name='Alice'); t2 = Teacher(name='Bob'); t3 = Teacher(name='Charlie')
            db.session.add_all([t1,t2,t3]); db.session.commit()
            s1 = Subject(name='Math', weekly_classes=4, teacher_id=t1.id)
            s2 = Subject(name='Physics', weekly_classes=3, teacher_id=t2.id)
            s3 = Subject(name='Chemistry', weekly_classes=3, teacher_id=t3.id)
            db.session.add_all([s1,s2,s3])
            db.session.add_all([
                Room(name='R-101',capacity=60),
                Room(name='R-102',capacity=50),
                Room(name='Lab-1',capacity=30)
            ])
            db.session.commit()

    app.run(debug=True)

