import os
import subprocess
import json
import threading
import time
import psutil
import pynvml
import random
import signal # NEW: Import signal for process group termination
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash

# --- Basic Configuration ---
# MODIFIED: Use /app/uploads for Render's persistent disk
UPLOAD_FOLDER = os.environ.get('RENDER_DISK_PATH', 'uploads')
STREAMS_CONFIG_FILE = os.path.join(UPLOAD_FOLDER, 'streams.json')
SECRET_KEY = os.environ.get('SECRET_KEY', 'a-very-secret-and-hard-to-guess-key')
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'Admin@123')
PORT = int(os.environ.get('PORT', 10000))

# --- App Initialization ---
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['SECRET_KEY'] = SECRET_KEY
# Ensure the mount point exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# In-memory store for active FFmpeg processes {stream_id: Popen_object}
ACTIVE_STREAMS = {}

# --- GPU Utilities (Graceful handling if no NVIDIA GPU) ---
def get_gpu_usage():
    try:
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
        return {
            "gpu_util": f"{util.gpu}%",
            "mem_util": f"{util.memory}%",
            "mem_total": f"{memory.total // 1024**2}MB",
            "mem_used": f"{memory.used // 1024**2}MB",
        }
    except Exception:
        return None

# --- Data Persistence ---
def load_stream_configs():
    if not os.path.exists(STREAMS_CONFIG_FILE):
        return {}
    with open(STREAMS_CONFIG_FILE, 'r') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_stream_configs(configs):
    with open(STREAMS_CONFIG_FILE, 'w') as f:
        json.dump(configs, f, indent=4)

# --- Authentication ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- FFmpeg Process Management ---

# NEW: Function to log FFmpeg's output in a separate thread
def log_stream_output(stream_id, process):
    """Reads and prints the stderr from the ffmpeg process."""
    # Use process.stderr, as ffmpeg logs its progress to stderr
    for line in iter(process.stderr.readline, b''):
        print(f"[ffmpeg:{stream_id}] {line.decode('utf-8').strip()}", flush=True)

def start_stream(stream_id, config):
    if stream_id in ACTIVE_STREAMS and ACTIVE_STREAMS[stream_id].poll() is None:
        print(f"Stream {stream_id} is already running.")
        return

    input_path = config['input']
    rtmp_urls = config['rtmp_urls']

    command = [
        'ffmpeg', '-re', '-stream_loop', '-1', '-i', input_path,
        '-c:a', 'copy',  # Copy the audio stream as-is
        '-c:v', 'copy',  # Copy the video stream as-is
        '-bsf:v', 'h264_mp4toannexb', # APPLY THE FIX: Add the bitstream filter for video
        '-f', 'tee', '-map', '0:v?', '-map', '0:a?',
    ]
    tee_str = "|".join([f"[f=flv]{url}" for url in rtmp_urls])
    command.append(tee_str)

    print(f"Starting stream {stream_id} with command: {' '.join(command)}", flush=True)
    try:
        # MODIFIED: Detach the FFmpeg process from the Gunicorn worker
        # - start_new_session=True makes it an independent process group.
        # - stderr=subprocess.PIPE allows us to capture logs.
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True # This is the magic key to detaching the process
        )
        ACTIVE_STREAMS[stream_id] = process
        print(f"Stream {stream_id} started with PID: {process.pid}", flush=True)

        # NEW: Start a thread to monitor and log FFmpeg's output without blocking
        log_thread = threading.Thread(target=log_stream_output, args=(stream_id, process))
        log_thread.daemon = True # Allows main program to exit even if thread is running
        log_thread.start()

    except Exception as e:
        print(f"Error starting stream {stream_id}: {e}", flush=True)

def stop_stream(stream_id):
    process = ACTIVE_STREAMS.pop(stream_id, None)
    if process:
        print(f"Stopping stream {stream_id} with PID: {process.pid}", flush=True)
        try:
            # MODIFIED: Terminate the entire process group started by FFmpeg
            # This is the proper way to kill a process started with start_new_session=True
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
            process.wait(timeout=5)
            print(f"Stream {stream_id} terminated gracefully.", flush=True)
        except (ProcessLookupError, PermissionError):
            print(f"Process for stream {stream_id} already gone.", flush=True)
        except subprocess.TimeoutExpired:
            print(f"Stream {stream_id} did not terminate gracefully, killing.", flush=True)
            os.killpg(pgid, signal.SIGKILL)
    else:
        print(f"Stream {stream_id} not found in active streams.", flush=True)


# --- Flask Routes (No changes needed below this line) ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            flash('Login successful!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials. Please try again.', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    configs = load_stream_configs()
    for stream_id, config in configs.items():
        process = ACTIVE_STREAMS.get(stream_id)
        if process and process.poll() is None:
            config['status'] = 'Running'
            config['pid'] = process.pid
        else:
            config['status'] = 'Stopped'
            if stream_id in ACTIVE_STREAMS:
                del ACTIVE_STREAMS[stream_id]

    cpu_usage = psutil.cpu_percent(interval=0.1)
    ram_usage = psutil.virtual_memory().percent
    gpu_usage = get_gpu_usage()

    return render_template('dashboard.html', streams=configs, cpu=cpu_usage, ram=ram_usage, gpu=gpu_usage)

@app.route('/add_stream', methods=['POST'])
@login_required
def add_stream():
    configs = load_stream_configs()
    stream_id = f"stream_{int(time.time())}"
    
    input_type = request.form['input_type']
    stream_name = request.form.get('stream_name', f'Stream {len(configs)+1}')
    rtmp_urls = [url.strip() for url in request.form['rtmp_urls'].splitlines() if url.strip()]
    
    if not rtmp_urls:
        flash('At least one RTMP URL is required.', 'danger')
        return redirect(url_for('dashboard'))

    input_path = ''
    if input_type == 'url':
        input_path = request.form['video_url']
    elif input_type == 'upload':
        if 'video_file' not in request.files or not request.files['video_file'].filename:
            flash('No file selected for upload.', 'danger')
            return redirect(url_for('dashboard'))
        file = request.files['video_file']
        filename = f"{stream_id}_{file.filename.replace(' ', '_')}"
        input_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(input_path)
    
    configs[stream_id] = {
        'id': stream_id, 'name': stream_name, 'input': input_path,
        'input_type': input_type, 'rtmp_urls': rtmp_urls
    }
    save_stream_configs(configs)
    flash(f"Stream '{stream_name}' added successfully.", "success")
    return redirect(url_for('dashboard'))

@app.route('/action/start/<stream_id>', methods=['POST'])
@login_required
def handle_start(stream_id):
    configs = load_stream_configs()
    if stream_id in configs:
        start_stream(stream_id, configs[stream_id])
        flash(f"Attempting to start stream '{configs[stream_id]['name']}'.", "info")
    else:
        flash("Stream not found.", "danger")
    return redirect(url_for('dashboard'))

@app.route('/action/stop/<stream_id>', methods=['POST'])
@login_required
def handle_stop(stream_id):
    configs = load_stream_configs()
    stop_stream(stream_id)
    flash(f"Stream '{configs.get(stream_id, {}).get('name', stream_id)}' stopped.", "info")
    return redirect(url_for('dashboard'))

@app.route('/action/delete/<stream_id>', methods=['POST'])
@login_required
def handle_delete(stream_id):
    stop_stream(stream_id)
    configs = load_stream_configs()
    config_to_delete = configs.pop(stream_id, None)
    
    if config_to_delete:
        if config_to_delete.get('input_type') == 'upload':
            file_path = config_to_delete.get('input')
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError as e:
                    print(f"Error removing file {file_path}: {e}", flush=True)

        save_stream_configs(configs)
        flash(f"Stream '{config_to_delete['name']}' deleted.", "success")
    else:
        flash("Stream not found.", "danger")
    return redirect(url_for('dashboard'))

@app.route('/healthz')
def health_check():
    return 'OK', 200

@app.route('/wakeup')
def wakeup():
    num1 = random.randint(1, 100)
    num2 = random.randint(1, 100)
    return jsonify({
        'status': 'awake', 'message': 'Service is active.',
        'task': f'What is {num1} + {num2}?', 'answer': num1 + num2
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=True)
