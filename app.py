import os
import subprocess
import json
import threading
import time
import psutil
import pynvml
import random
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash

# --- Basic Configuration ---
UPLOAD_FOLDER = 'uploads'
STREAMS_CONFIG_FILE = 'streams.json'
SECRET_KEY = os.environ.get('SECRET_KEY', 'a-very-secret-and-hard-to-guess-key')
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'Admin@123')
PORT = int(os.environ.get('PORT', 10000)) # Render.com uses the PORT env var

# --- App Initialization ---
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['SECRET_KEY'] = SECRET_KEY
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
        return None # No GPU or nvidia-ml-py not installed

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
def start_stream(stream_id, config):
    if stream_id in ACTIVE_STREAMS:
        print(f"Stream {stream_id} is already running.")
        return

    input_path = config['input']
    rtmp_urls = config['rtmp_urls']

    # The magic command: -c copy avoids re-encoding, -stream_loop -1 loops indefinitely
    # The 'tee' muxer allows streaming to multiple destinations simultaneously
    # '-re' reads the input at its native frame rate, crucial for streaming files.
    command = [
        'ffmpeg',
        '-re',
        '-stream_loop', '-1',
        '-i', input_path,
        '-c', 'copy',
        '-f', 'tee',
        '-map', '0:v?', '-map', '0:a?', # Map video and audio streams
    ]
    
    # Format the tee muxer string for multiple outputs
    tee_str = "|".join([f"[f=flv]{url}" for url in rtmp_urls])
    command.append(tee_str)

    print(f"Starting stream {stream_id} with command: {' '.join(command)}")
    try:
        # Start the process in the background
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ACTIVE_STREAMS[stream_id] = process
        print(f"Stream {stream_id} started with PID: {process.pid}")
    except Exception as e:
        print(f"Error starting stream {stream_id}: {e}")

def stop_stream(stream_id):
    process = ACTIVE_STREAMS.pop(stream_id, None)
    if process:
        try:
            process.terminate() # Send SIGTERM
            process.wait(timeout=5) # Wait for graceful shutdown
            print(f"Stream {stream_id} terminated gracefully.")
        except subprocess.TimeoutExpired:
            process.kill() # Force kill if it doesn't terminate
            print(f"Stream {stream_id} killed.")
    else:
        print(f"Stream {stream_id} not found in active streams.")


# --- Flask Routes ---

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
    # Augment configs with runtime status
    for stream_id, config in configs.items():
        process = ACTIVE_STREAMS.get(stream_id)
        if process and process.poll() is None: # poll() is None if process is running
            config['status'] = 'Running'
            config['pid'] = process.pid
        else:
            config['status'] = 'Stopped'
            # Clean up dead processes from ACTIVE_STREAMS
            if stream_id in ACTIVE_STREAMS:
                del ACTIVE_STREAMS[stream_id]

    # System Usage
    cpu_usage = psutil.cpu_percent(interval=0.1)
    ram_usage = psutil.virtual_memory().percent
    gpu_usage = get_gpu_usage()

    return render_template('dashboard.html', streams=configs, cpu=cpu_usage, ram=ram_usage, gpu=gpu_usage)

@app.route('/add_stream', methods=['POST'])
@login_required
def add_stream():
    configs = load_stream_configs()
    stream_id = f"stream_{int(time.time())}" # Unique ID based on timestamp
    
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
        filename = f"{stream_id}_{file.filename}"
        input_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(input_path)
    
    configs[stream_id] = {
        'id': stream_id,
        'name': stream_name,
        'input': input_path,
        'input_type': input_type,
        'rtmp_urls': rtmp_urls
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
    stop_stream(stream_id) # Ensure it's stopped before deleting
    configs = load_stream_configs()
    config_to_delete = configs.pop(stream_id, None)
    
    if config_to_delete:
        # If it was an uploaded file, delete it
        if config_to_delete.get('input_type') == 'upload':
            file_path = config_to_delete.get('input')
            if os.path.exists(file_path):
                os.remove(file_path)
        save_stream_configs(configs)
        flash(f"Stream '{config_to_delete['name']}' deleted.", "success")
    else:
        flash("Stream not found.", "danger")
    return redirect(url_for('dashboard'))

# --- Health and Wakeup Endpoints ---

@app.route('/healthz')
def health_check():
    """Render health check endpoint."""
    return 'OK', 200

@app.route('/wakeup')
def wakeup():
    """
    Keep-alive endpoint for cron jobs.
    Returns a simple random math problem to simulate activity.
    """
    num1 = random.randint(1, 100)
    num2 = random.randint(1, 100)
    return jsonify({
        'status': 'awake',
        'message': 'Service is active.',
        'task': f'What is {num1} + {num2}?',
        'answer': num1 + num2
    })

# --- Main Execution ---
if __name__ == '__main__':
    # On Render, Gunicorn will run the app. This is for local development.
    app.run(host='0.0.0.0', port=PORT, debug=True)
