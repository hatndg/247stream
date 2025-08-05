from flask import Flask, render_template, request, redirect, session, send_from_directory, flash, url_for
import os
import subprocess
import json
import hashlib
import threading
import time
import uuid
import psutil
# Thư viện cho mã hóa
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.backends import default_backend
import base64

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey_for_flask_session")

# --- Constants ---
UPLOAD_FOLDER = "uploads"
PASS_FILE = "password.txt"
STREAMS_FILE = "streams.json"
BACKUP_FILE = "streams_backup.json" # File backup được mã hóa
DEFAULT_PASS = "Admin@123"
PROCESSES = {}

# --- Setup ---
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# === Encryption Helper Functions ===

def derive_key(password: str, salt: bytes) -> bytes:
    """Tạo key an toàn từ password và salt."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
        backend=default_backend()
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))

def encrypt_data(data_dict: dict, password: str) -> dict:
    """Mã hóa một dictionary và trả về salt + data đã mã hóa."""
    salt = os.urandom(16)
    key = derive_key(password, salt)
    f = Fernet(key)
    
    json_data = json.dumps(data_dict).encode()
    encrypted_data = f.encrypt(json_data)
    
    return {
        "salt": base64.b64encode(salt).decode('utf-8'),
        "data": base64.b64encode(encrypted_data).decode('utf-8')
    }

def decrypt_data(encrypted_bundle: dict, password: str) -> dict | None:
    """Giải mã dữ liệu từ bundle và password."""
    try:
        salt = base64.b64decode(encrypted_bundle['salt'])
        encrypted_data = base64.b64decode(encrypted_bundle['data'])
        key = derive_key(password, salt)
        f = Fernet(key)
        
        decrypted_json = f.decrypt(encrypted_data)
        return json.loads(decrypted_json)
    except (InvalidToken, KeyError, TypeError):
        return None

# === Core App Logic ===

def hash_pass(p): return hashlib.sha256(p.encode()).hexdigest()

if not os.path.exists(PASS_FILE):
    with open(PASS_FILE, "w") as f: f.write(hash_pass(DEFAULT_PASS))

def load_streams():
    if os.path.exists(STREAMS_FILE):
        try:
            with open(STREAMS_FILE) as f: return json.load(f)
        except json.JSONDecodeError:
            return []
    return []

def save_streams(data):
    with open(STREAMS_FILE, "w") as f: json.dump(data, f, indent=2)

def create_encrypted_backup(password: str):
    """Tạo file backup được mã hóa từ streams.json hiện tại."""
    streams = load_streams()
    encrypted_bundle = encrypt_data(streams, password)
    with open(BACKUP_FILE, 'w') as f:
        json.dump(encrypted_bundle, f)
    print("Encrypted backup created successfully.")

def start_stream_thread(stream_info):
    """Bọc logic khởi động thread để tái sử dụng."""
    stream_id = stream_info['id']
    if stream_id in PROCESSES and PROCESSES[stream_id].is_alive():
        print(f"Stream {stream_id} is already running.")
        return
    
    p = threading.Thread(target=stream_loop, args=(
        stream_id, 
        stream_info['src'], 
        stream_info['dests'], 
        stream_info['loop']
    ), daemon=True)
    PROCESSES[stream_id] = p
    p.start()
    print(f"Started stream thread for {stream_id}")

# === Routes ===

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = request.form["password"]
        with open(PASS_FILE) as f:
            real_hash = f.read()
        if hash_pass(pw) == real_hash:
            session["logged"] = True
            session["first"] = (pw == DEFAULT_PASS)
            session["user_pass"] = pw 
            flash("Login successful!", "success")
            return redirect("/change" if session["first"] else "/")
        flash("Wrong password", "danger")
        return redirect(url_for('login'))
    return render_template("login.html")

@app.route("/change", methods=["GET", "POST"])
def change():
    if not session.get("logged"): return redirect("/login")
    if request.method == "POST":
        new_pass = request.form["newpass"]
        with open(PASS_FILE, "w") as f: f.write(hash_pass(new_pass))
        session["first"] = False
        session["user_pass"] = new_pass
        create_encrypted_backup(new_pass)
        flash("Password changed successfully. A new encrypted backup has been created.", "success")
        return redirect("/")
    return render_template("change.html")

@app.route("/ping.js")
def fake_js():
    return 'console.log("Live247 initialized");', 200, {"Content-Type": "application/javascript"}


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/", methods=["GET", "POST"])
def index():
    if not session.get("logged"): return redirect("/login")
    
    if request.method == "POST":
        streams = load_streams()
        name = request.form["name"]
        source = request.form["source"]
        loop = request.form.get("loop") == "on"
        dests = [d.strip() for d in request.form["destinations"].splitlines() if d.strip()]

        filename = ""
        if "video" in request.files and request.files["video"].filename:
            f = request.files["video"]
            filename = os.path.join(UPLOAD_FOLDER, f.filename)
            f.save(filename)
        elif source.startswith(("http", "rtmp", "rtsp")):
            filename = source

        if not filename:
            flash("You must provide either a video file or a source URL.", "danger")
            return redirect(url_for('index'))

        stream_id = str(uuid.uuid4())
        info = {"id": stream_id, "name": name, "src": filename, "loop": loop, "dests": dests}
        streams.append(info)
        save_streams(streams)
        start_stream_thread(info)
        
        flash(f"Stream '{name}' started!", "success")
        return redirect("/")

    streams_with_status = []
    for s in load_streams():
        s['status'] = 'Running' if s['id'] in PROCESSES and PROCESSES[s['id']].is_alive() else 'Stopped'
        streams_with_status.append(s)
    return render_template("index.html", streams=streams_with_status)

# *** HÀM STOP ĐÃ ĐƯỢC CẢI TIẾN ***
@app.route("/stop/<sid>")
def stop(sid):
    if not session.get("logged"): return redirect("/login")
    
    streams = load_streams()
    stream_to_stop = next((s for s in streams if s["id"] == sid), None)

    if stream_to_stop:
        source_path = stream_to_stop['src']
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            # Kiểm tra cả tên tiến trình và dòng lệnh để chắc chắn
            if proc.info['name'].lower().startswith('ffmpeg'):
                try:
                    # Kiểm tra xem đường dẫn nguồn có trong dòng lệnh của tiến trình không
                    if source_path in " ".join(proc.info['cmdline']):
                        print(f"Stopping process {proc.pid} for stream {sid} with source {source_path}")
                        proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

    # Xóa khỏi danh sách streams trong file JSON
    streams_to_keep = [s for s in streams if s["id"] != sid]
    save_streams(streams_to_keep)
    
    # Xóa khỏi danh sách tiến trình đang chạy trong bộ nhớ
    if sid in PROCESSES:
        del PROCESSES[sid]
        
    flash(f"Stream has been stopped and removed.", "info")
    return redirect("/")

@app.route("/manage_backup", methods=["GET"])
def manage_backup_page():
    if not session.get("logged"): return redirect("/login")
    backup_exists = os.path.exists(BACKUP_FILE)
    return render_template("backup.html", backup_exists=backup_exists)

@app.route("/backup/download")
def download_backup():
    if not session.get("logged"): return redirect("/login")
    password = session.get("user_pass")
    if not password:
        flash("Session expired. Please login again to create a backup.", "warning")
        return redirect(url_for('login'))
    
    create_encrypted_backup(password)
    return send_from_directory(".", BACKUP_FILE, as_attachment=True)
    
@app.route("/backup/restore", methods=["POST"])
def restore_from_backup():
    if not session.get("logged"): return redirect("/login")
    
    password = request.form.get("password")
    backup_file = request.files.get("backup_file")

    if not password or not backup_file or not backup_file.filename:
        flash("Password and backup file are required.", "danger")
        return redirect(url_for('manage_backup_page'))

    try:
        encrypted_bundle = json.load(backup_file)
    except json.JSONDecodeError:
        flash("Invalid backup file format.", "danger")
        return redirect(url_for('manage_backup_page'))

    decrypted_streams = decrypt_data(encrypted_bundle, password)
    
    if decrypted_streams is None:
        flash("Failed to decrypt backup. Incorrect password or corrupted file.", "danger")
        return redirect(url_for('manage_backup_page'))
    
    current_sids = [s['id'] for s in load_streams()]
    for current_sid in current_sids:
        # Tái sử dụng hàm stop để dọn dẹp triệt để
        stop_stream_internally(current_sid)
        
    save_streams(decrypted_streams)
    for stream_info in decrypted_streams:
        start_stream_thread(stream_info)
        
    flash("Successfully restored streams from backup. All streams are restarting.", "success")
    return redirect(url_for('index'))

def stop_stream_internally(sid):
    """Hàm phụ để route restore sử dụng, không redirect"""
    stream_to_stop = next((s for s in load_streams() if s["id"] == sid), None)
    if stream_to_stop:
        source_path = stream_to_stop['src']
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            if proc.info['name'].lower().startswith('ffmpeg'):
                try:
                    if source_path in " ".join(proc.info['cmdline']):
                        proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
    if sid in PROCESSES:
        del PROCESSES[sid]
    
@app.route("/healthz")
def health(): return "<p>OK</p>", 200

@app.route("/uploads/<path:filename>")
def uploaded_file(filename): return send_from_directory(UPLOAD_FOLDER, filename)

# *** HÀM STREAM_LOOP ĐÃ ĐƯỢC SỬA LỖI ***
def stream_loop(sid, src, dests, loop):
    if not src:
        print(f"[{sid}] Source not specified.")
        return
    if not src.startswith(("http", "rtmp", "rtsp")) and not os.path.exists(src):
        print(f"[{sid}] File does not exist: {src}")
        return

    cmd_base = [
        "ffmpeg", "-re",
        "-i", src,
        "-c", "copy",
        "-map", "0",
        "-f", "flv",
        # THAY ĐỔI QUAN TRỌNG: Báo cho ffmpeg không cần ghi metadata cuối file
        "-flvflags", "no_duration_filesize"
    ]
    
    active_processes = []
    
    def cleanup():
        print(f"[{sid}] Cleaning up processes for stream.")
        for p in active_processes:
            if p.poll() is None:
                p.kill()
        if sid in PROCESSES:
            del PROCESSES[sid]

    while True:
        streams_now = load_streams()
        if not any(s['id'] == sid for s in streams_now):
            print(f"[{sid}] Stream was removed from config. Stopping loop.")
            break

        print(f"[{sid}] Starting FFmpeg stream (in copy mode)...")
        
        for d in dests:
            cmd = cmd_base + [d]
            print(f"[{sid}] Running: {' '.join(cmd)}")
            p = subprocess.Popen(cmd)
            active_processes.append(p)
        
        for p in active_processes:
            p.wait()
        
        active_processes.clear()

        if not loop:
            print(f"[{sid}] Stream finished and loop is disabled.")
            break

        print(f"[{sid}] FFmpeg exited, restarting due to loop=True")
        time.sleep(1)

    cleanup()
    final_streams = [s for s in load_streams() if s['id'] != sid]
    save_streams(final_streams)
    print(f"[{sid}] Stream loop and cleanup finished.")

def automatic_restore_on_startup():
    if not os.path.exists(BACKUP_FILE):
        print("No backup file found, skipping automatic restore.")
        return
    if not os.path.exists(PASS_FILE):
        print("Password file not found, cannot decrypt backup.")
        return

    print("Attempting to restore streams from backup...")
    try:
        with open(BACKUP_FILE, 'r') as f:
            encrypted_bundle = json.load(f)
        
        # Thử giải mã bằng mật khẩu mặc định
        decrypted_streams = decrypt_data(encrypted_bundle, DEFAULT_PASS)
        
        if decrypted_streams:
            with open(PASS_FILE, 'r') as pf:
                if hash_pass(DEFAULT_PASS) == pf.read():
                    print("Default password detected. Restoring streams from backup.")
                    save_streams(decrypted_streams)
                    for stream_info in decrypted_streams:
                        start_stream_thread(stream_info)
                else:
                    print("Backup was encrypted with default password, but current password has changed. Please restore manually.")
        else:
            print("Could not decrypt backup with default password. Please restore manually after login.")
    except Exception as e:
        print(f"Error during automatic restore: {e}")

if __name__ == "__main__":
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        automatic_restore_on_startup()
    app.run(host="0.0.0.0", port=10000)
