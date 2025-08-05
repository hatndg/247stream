from flask import Flask, render_template, request, redirect, session, send_from_directory
import os, subprocess, json, hashlib, threading, time, uuid, psutil

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey")

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PASS_FILE = "password.txt"
DEFAULT_PASS = "Admin@123"
STREAMS_FILE = "streams.json"
PROCESSES = {}

def hash_pass(p): return hashlib.sha256(p.encode()).hexdigest()

# Initialize password
if not os.path.exists(PASS_FILE):
    with open(PASS_FILE, "w") as f: f.write(hash_pass(DEFAULT_PASS))

def load_streams():
    if os.path.exists(STREAMS_FILE):
        with open(STREAMS_FILE) as f: return json.load(f)
    return []

def save_streams(data):
    with open(STREAMS_FILE, "w") as f: json.dump(data, f)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = request.form["password"]
        with open(PASS_FILE) as f:
            real = f.read()
        if hash_pass(pw) == real:
            session["logged"] = True
            session["first"] = (pw == DEFAULT_PASS)
            return redirect("/change" if session["first"] else "/")
        return "Wrong password", 403
    return render_template("login.html")

@app.route("/change", methods=["GET", "POST"])
def change():
    if not session.get("logged"): return redirect("/login")
    if request.method == "POST":
        new = request.form["newpass"]
        with open(PASS_FILE, "w") as f: f.write(hash_pass(new))
        session["first"] = False
        return redirect("/")
    return render_template("change.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/", methods=["GET", "POST"])
def index():
    if not session.get("logged"): return redirect("/login")
    streams = load_streams()

    if request.method == "POST":
        name = request.form["name"]
        source = request.form["source"]
        loop = request.form.get("loop") == "on"
        dests = [d.strip() for d in request.form["destinations"].splitlines() if d.strip()]

        filename = ""
        if "video" in request.files:
            f = request.files["video"]
            if f and f.filename:
                filename = os.path.join(UPLOAD_FOLDER, f.filename)
                f.save(filename)
        elif source.startswith("http"):
            filename = source

        stream_id = str(uuid.uuid4())
        info = {"id": stream_id, "name": name, "src": filename, "loop": loop, "dests": dests}
        streams.append(info)
        save_streams(streams)

        p = threading.Thread(target=stream_loop, args=(stream_id, filename, dests, loop))
        PROCESSES[stream_id] = p
        p.start()
        return redirect("/")

    return render_template("index.html", streams=streams)

@app.route("/stop/<sid>")
def stop(sid):
    streams = load_streams()
    streams = [s for s in streams if s["id"] != sid]
    save_streams(streams)

    for proc in psutil.process_iter(['pid', 'cmdline']):
        if sid in " ".join(proc.info['cmdline']):
            proc.kill()
    return redirect("/")

@app.route("/healthz")
def health(): return "<p>OK</p>", 200

@app.route("/uploads/<path:filename>")
def uploaded_file(filename): return send_from_directory(UPLOAD_FOLDER, filename)

def stream_loop(sid, src, dests, loop):
    if not src:
        print(f"[{sid}] Source not specified.")
        return
    if not os.path.exists(src) and not src.startswith("http"):
        print(f"[{sid}] File does not exist: {src}")
        return

    # Do file nguồn đã được chuẩn bị sẵn, chúng ta chỉ cần copy stream.
    # Việc này gần như không tốn CPU.
    cmd_base = [
        "ffmpeg", "-re",
        "-i", src,
        # QUAN TRỌNG: copy codec video và audio, không mã hóa lại
        "-c:v", "copy",
        "-c:a", "copy",
        "-f", "flv"
    ]

    while True:
        print(f"[{sid}] Starting FFmpeg stream (in copy mode)...")
        processes = []

        for d in dests:
            # Tạo lệnh đầy đủ cho từng đích đến
            cmd = cmd_base + [d]
            print(f"[{sid}] Running: {' '.join(cmd)}")
            p = subprocess.Popen(cmd)
            processes.append(p)

        # Wait for all FFmpeg processes to exit
        for p in processes:
            p.wait()

        if not loop:
            print(f"[{sid}] Stream finished and loop is disabled.")
            break

        print(f"[{sid}] FFmpeg exited, restarting due to loop=True")
        time.sleep(1)

    # Dọn dẹp stream khỏi danh sách khi vòng lặp kết thúc
    print(f"[{sid}] Cleaning up stream from active processes.")
    if sid in PROCESSES:
        del PROCESSES[sid]
    streams = load_streams()
    streams_to_keep = [s for s in streams if s.get("id") != sid]
    save_streams(streams_to_keep)

    # Dọn dẹp stream khỏi danh sách khi vòng lặp kết thúc
    print(f"[{sid}] Cleaning up stream from active processes.")
    if sid in PROCESSES:
        del PROCESSES[sid]
    streams = load_streams()
    streams_to_keep = [s for s in streams if s.get("id") != sid]
    save_streams(streams_to_keep)



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
