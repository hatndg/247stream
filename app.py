def stream_loop(sid, src, dests, loop):
    if not src:
        print(f"[{sid}] Source not specified.")
        return
    if not os.path.exists(src) and not src.startswith("http"):
        print(f"[{sid}] File does not exist: {src}")
        return

    while True:
        print(f"[{sid}] Starting FFmpeg stream...")
        processes = []

        for d in dests:
            # Sửa đổi lệnh ffmpeg tại đây
            cmd = [
                "ffmpeg", "-re",
                "-i", src,
                
                # --- Video Options ---
                "-c:v", "libx264",
                # Thay đổi preset thành "superfast" nếu "veryfast" vẫn quá nặng
                # Các lựa chọn: veryfast, superfast, ultrafast
                "-preset", "ultrafast", 
                "-tune", "zerolatency",
                # Đặt tần suất keyframe (GOP size) để đáp ứng yêu cầu của YouTube
                "-g", "60", # 2 giây/keyframe nếu video là 30fps
                # THÊM CÁC DÒNG NÀY: Thiết lập bitrate để luồng ổn định
                "-b:v", "2000k",      # Bitrate mục tiêu (có thể giảm xuống 1500k nếu cần)
                "-maxrate", "2500k",  # Bitrate tối đa
                "-bufsize", "4000k",  # Kích thước bộ đệm
                
                # --- Audio Options ---
                "-c:a", "aac", 
                "-ar", "44100", 
                "-b:a", "128k",
                
                # --- Output Options ---
                "-f", "flv", d
            ]
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
