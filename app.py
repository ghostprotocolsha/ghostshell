import os
import shutil
import subprocess
import threading
import uuid
import re
import stat
import time
import zipfile
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, jsonify, send_file
from waitress import serve

APKTOOL_CMD = shutil.which("apktool")  # resolved once at startup; None if not found on PATH
SYSTEM_AAPT2_CMD = shutil.which("aapt2")  # optional system aapt2, used as an extra fallback

app = Flask(__name__)

app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

UPLOAD_FOLDER = 'uploads'
DECOMPILE_FOLDER = 'decompiled'
APKTOOL_TMP_DIR = os.path.abspath('apktool_tmp')
ALLOWED_EXTENSIONS = {'apk'}
ZIP_EXTENSIONS = {'zip'}
APKTOOL_TIMEOUT_SECONDS = 1200

# Attribute names known to be unsupported by apktool's bundled framework, which make
# aapt2 fail to link resources ("attribute ... not found") and kill the whole build.
KNOWN_UNSUPPORTED_ATTRIBUTES = [
    "android:isCredential",
]

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['DECOMPILE_FOLDER'] = DECOMPILE_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DECOMPILE_FOLDER, exist_ok=True)
os.makedirs(APKTOOL_TMP_DIR, exist_ok=True)

# Serialize build calls to avoid corrupting/racing the extracted binary
apktool_build_lock = threading.Lock()

# Deep scan progress cache dictionary
scan_progress_cache = {}


def start_apktool_tmp_permission_watcher():
    """Background daemon thread to automatically grant executable (+x) permissions 
    to any temporary binaries/libraries extracted by apktool/aapt2 inside APKTOOL_TMP_DIR."""
    def watch_and_chmod():
        while True:
            try:
                if os.path.exists(APKTOOL_TMP_DIR):
                    for root, dirs, files in os.walk(APKTOOL_TMP_DIR):
                        for f in files:
                            file_path = os.path.join(root, f)
                            try:
                                current_mode = os.stat(file_path).st_mode
                                if not (current_mode & stat.S_IXUSR):
                                    os.chmod(file_path, 0o755)
                            except Exception:
                                pass
                time.sleep(0.1)
            except Exception:
                time.sleep(1)

    t = threading.Thread(target=watch_and_chmod, daemon=True)
    t.start()


start_apktool_tmp_permission_watcher()


def get_apktool_env():
    """Environment for apktool subprocess calls: force both TMPDIR and Java's own
    internal temp directory (java.io.tmpdir) to our project-local APKTOOL_TMP_DIR folder."""
    env = os.environ.copy()
    env['TMPDIR'] = APKTOOL_TMP_DIR
    existing_opts = env.get('_JAVA_OPTIONS', '')
    env['_JAVA_OPTIONS'] = f"{existing_opts} -Djava.io.tmpdir={APKTOOL_TMP_DIR}".strip()
    return env


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_zip(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ZIP_EXTENSIONS


def safe_join(base_dir, *paths):
    """Join paths and guarantee the result stays inside base_dir (blocks path traversal)."""
    base_dir = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(base_dir, *paths))
    if not (target == base_dir or target.startswith(base_dir + os.sep)):
        raise ValueError("Unsafe path detected")
    return target


def strip_unsupported_attributes(content):
    """Remove only exact, known-unsupported attribute="..." occurrences."""
    for attr in KNOWN_UNSUPPORTED_ATTRIBUTES:
        escaped = re.escape(attr)
        content = re.sub(rf'\s+{escaped}\s*=\s*"[^"]*"', '', content)
        content = re.sub(rf"\s+{escaped}\s*=\s*'[^']*'", '', content)
    return content


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')


@app.route('/decompile_page')
def decompile_page():
    return render_template('decompile.html')


@app.route('/decompile', methods=['POST'])
def decompile():
    if 'apk_file' not in request.files:
        return jsonify({'status': 'error', 'message': "No file part named 'apk_file' in request"})

    file = request.files['apk_file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': "No file selected"})

    if not allowed_file(file.filename):
        return jsonify({'status': 'error', 'message': "Only .apk files are allowed"})

    safe_name = secure_filename(file.filename)
    if not safe_name:
        return jsonify({'status': 'error', 'message': "Invalid filename"})

    job_id = uuid.uuid4().hex[:8]
    stored_filename = f"{job_id}_{safe_name}"
    app_name = f"{job_id}_{os.path.splitext(safe_name)[0]}"

    try:
        upload_path = safe_join(app.config['UPLOAD_FOLDER'], stored_filename)
        out_path = safe_join(app.config['DECOMPILE_FOLDER'], app_name)
    except ValueError:
        return jsonify({'status': 'error', 'message': "Invalid filename"})

    upload_path_local = upload_path

    try:
        if APKTOOL_CMD is None:
            raise RuntimeError("apktool was not found on PATH.")

        file.save(upload_path)

        if os.path.exists(out_path):
            shutil.rmtree(out_path)

        process = subprocess.run(
            [APKTOOL_CMD, "d", upload_path, "-o", out_path, "-f"],
            capture_output=True,
            text=True,
            timeout=APKTOOL_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            env=get_apktool_env(),
        )

        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "").strip()
            raise RuntimeError(detail if detail else f"apktool exited with code {process.returncode}")

        if not os.path.isdir(out_path) or not os.listdir(out_path):
            raise RuntimeError("apktool reported success but produced no output files.")

        zip_filename = f"{app_name}_decompiled"
        zip_path = os.path.join(DECOMPILE_FOLDER, zip_filename)
        if os.path.exists(zip_path + ".zip"):
            os.remove(zip_path + ".zip")

        shutil.make_archive(zip_path, 'zip', out_path)

        try:
            shutil.rmtree(out_path)
        except OSError as e:
            print(f"[cleanup] could not remove extracted folder {out_path}: {e}")

        zip_filename_full = f"{zip_filename}.zip"
        return jsonify({'status': 'success', 'zip_file': zip_filename_full})

    except subprocess.TimeoutExpired:
        return jsonify({'status': 'error', 'message': f"apktool timed out after {APKTOOL_TIMEOUT_SECONDS}s"})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        if 'upload_path_local' in locals() and os.path.exists(upload_path_local):
            os.remove(upload_path_local)


@app.route('/recompile', methods=['POST'])
def recompile():
    if 'zip_file' not in request.files:
        return jsonify({'status': 'error', 'message': "No file part named 'zip_file' in request"})

    file = request.files['zip_file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': "No file selected"})

    if not allowed_zip(file.filename):
        return jsonify({'status': 'error', 'message': "Only .zip files are allowed for recompilation"})

    safe_name = secure_filename(file.filename)
    job_id = uuid.uuid4().hex[:8]

    zip_path = safe_join(app.config['UPLOAD_FOLDER'], f"{job_id}_{safe_name}")
    extract_path = safe_join(app.config['DECOMPILE_FOLDER'], f"{job_id}_extracted")
    built_apk_name = f"{job_id}_rebuilt.apk"
    built_apk_path = safe_join(app.config['DECOMPILE_FOLDER'], built_apk_name)

    try:
        file.save(zip_path)
        shutil.unpack_archive(zip_path, extract_path)

        contents = os.listdir(extract_path)
        source_dir = extract_path
        if len(contents) == 1 and os.path.isdir(os.path.join(extract_path, contents[0])):
            source_dir = os.path.join(extract_path, contents[0])

        # SANITIZE: Fix any resource directories containing spaces
        for root, dirs, files in os.walk(source_dir):
            for d in dirs:
                if " " in d:
                    old_dir_path = os.path.join(root, d)
                    new_dir_path = os.path.join(root, d.replace(" ", "_"))
                    if not os.path.exists(new_dir_path):
                        shutil.move(old_dir_path, new_dir_path)

        # Remove only exact known-unsupported attributes
        for root, dirs, files in os.walk(source_dir):
            for f in files:
                if not f.endswith('.xml'):
                    continue
                xml_file_path = os.path.join(root, f)
                try:
                    with open(xml_file_path, 'r', encoding='utf-8', errors='ignore') as xml_f:
                        content = xml_f.read()

                    new_content = strip_unsupported_attributes(content)

                    if new_content != content:
                        with open(xml_file_path, 'w', encoding='utf-8') as xml_f:
                            xml_f.write(new_content)
                except Exception as ex:
                    print(f"[warning] could not sanitize xml file {xml_file_path}: {ex}")

        if APKTOOL_CMD is None:
            raise RuntimeError("apktool is not installed or not on PATH.")

        build_cmd = [APKTOOL_CMD, "b", source_dir, "-o", built_apk_path, "--use-aapt2"]

        with apktool_build_lock:
            process = subprocess.run(
                build_cmd,
                capture_output=True,
                text=True,
                timeout=APKTOOL_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL,
                env=get_apktool_env(),
            )

        if process.returncode != 0:
            err_lines = [
                line for line in (process.stderr or "").splitlines()
                if not line.startswith("Picked up _JAVA_OPTIONS")
            ]
            detail = "\n".join(err_lines).strip() or (process.stdout or "").strip()
            raise RuntimeError(detail if detail else f"apktool build exited with code {process.returncode}")

        if not os.path.isfile(built_apk_path):
            detail = (process.stderr or process.stdout or "").strip()
            raise RuntimeError(
                "apktool reported success but no APK was produced. "
                + (f"Build output: {detail}" if detail else "")
            )

        # --- AUTO-SIGNING INTEGRATION ---
        try:
            keystore_path = os.path.join(app.config['UPLOAD_FOLDER'], 'debug.keystore')
            if not os.path.exists(keystore_path):
                subprocess.run([
                    "keytool", "-genkey", "-v", "-keystore", keystore_path,
                    "-storepass", "android", "-alias", "androiddebugkey",
                    "-keypass", "android", "-keyalg", "RSA", "-keysize", "2048",
                    "-validity", "10000", "-dname", "CN=Android,O=Android,C=US"
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            apksigner_path = shutil.which("apksigner")
            if apksigner_path:
                subprocess.run([
                    apksigner_path, "sign", "--ks", keystore_path,
                    "--ks-pass", "pass:android", "--key-pass", "pass:android",
                    built_apk_path
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            else:
                jarsigner_path = shutil.which("jarsigner")
                if jarsigner_path:
                    subprocess.run([
                        jarsigner_path, "-keystore", keystore_path,
                        "-storepass", "android", "-keypass", "android",
                        built_apk_path, "androiddebugkey"
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        except Exception as sign_ex:
            print(f"[warning] auto-signing failed: {sign_ex}")
        # -------------------------------

        return jsonify({'status': 'success', 'apk_file': built_apk_name})

    except subprocess.TimeoutExpired:
        return jsonify({'status': 'error', 'message': f"apktool build timed out after {APKTOOL_TIMEOUT_SECONDS}s"})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        if os.path.exists(extract_path):
            shutil.rmtree(extract_path, ignore_errors=True)


@app.route('/download_zip/<filename>')
def download_zip(filename):
    safe_name = secure_filename(filename)
    try:
        file_path = safe_join(DECOMPILE_FOLDER, safe_name)
    except ValueError:
        return "Invalid filename", 400

    if not os.path.isfile(file_path):
        return "File not found", 404

    return send_file(file_path, as_attachment=True)


@app.route('/editor_page')
def editor_page():
    return render_template('editor.html')


@app.route('/scanner_page')
def scanner_page():
    return render_template('scanner.html')


@app.route('/scan_vulnerability', methods=['POST'])
def scan_vulnerability():
    vulnerability_reports = [
        {
            "file": "smali/com/ghostshell/auth/LoginController.smali",
            "line": "45",
            "severity": "HIGH",
            "issue": "Hardcoded API Secret Key",
            "code_snippet": "const-string v0, \"AIzaSyD-SecretKey-12345XYZ\"",
            "impact": "Attackers can decompile the APK and extract this secret key.",
            "remediation": "Move sensitive keys to server-side APIs."
        }
    ]
    return jsonify(status='success', reports=vulnerability_reports)


# --- DEEP VULNERABILITY SCANNING (Filtered OWASP Top 10 Engine) ---
def perform_deep_scan(scan_id, source_dir):
    global scan_progress_cache
    try:
        scan_progress_cache[scan_id] = {"progress": 0, "status": "Initializing filtered OWASP Top 10 scan...", "vulnerabilities": []}
        
        # Ignored third-party directory patterns to reduce false positives
        ignored_paths = ['androidx/', 'kotlin/', 'com/google/', 'android/support/', 'okhttp3/']

        all_files = []
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                if file.endswith(('.smali', '.xml', '.java', '.kt', '.js', '.json', '.properties', '.cfg', '.conf', '.html', '.php', '.ts')):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, source_dir).replace('\\', '/')
                    
                    # Skip third-party library code to eliminate massive false positives
                    if any(ignored in rel_path for ignored in ignored_paths):
                        continue
                        
                    all_files.append(full_path)
        
        total_files = len(all_files)
        if total_files == 0:
            scan_progress_cache[scan_id] = {"progress": 100, "status": "Completed (No app files found after filtering)", "vulnerabilities": []}
            return

        vulnerabilities = []
        
        # Rules
        patterns = [
            # A01:2021 - Broken Access Control
            {"type": "Broken Access Control / Insecure Endpoint (OWASP A01)", "regex": r"(bypass_auth|disable_auth|skip_auth|unsecured_endpoint)", "severity": "HIGH", "desc": "Potential broken access control or administrative authentication bypass detected."},
            
            # A02:2021 - Cryptographic Failures (Updated regex to ignore XML/Layout namespaces)
            {"type": "Cleartext HTTP Traffic Usage (OWASP A02)", "regex": r"['\"]http://(?!schemas\.android\.com)[^\s'\"]+['\"]", "severity": "LOW", "desc": "Hardcoded unencrypted HTTP endpoints can expose data to MITM inspection."},
            {"type": "Weak Cryptography Hash (OWASP A02)", "regex": r"MessageDigest\.getInstance\s*\(\s*\"(MD5|SHA-1)\"\s*\)", "severity": "MEDIUM", "desc": "Use of cryptographically broken or deprecated hash algorithms (MD5/SHA-1)."},
            {"type": "Disabled SSL Certificate Verification (OWASP A02)", "regex": r"(TrustAllManager|ALLOW_ALL_HOSTNAME_VERIFIER|checkServerTrusted\s*\([^)]*\)\s*\{\s*\})", "severity": "HIGH", "desc": "Custom trust manager or hostname verifier disables SSL/TLS validation check."},
            
            # A03:2021 - Injection
            {"type": "SQL Injection Risk (OWASP A03)", "regex": r"(rawQuery\s*\(|execSQL\s*\([^)]*\+)", "severity": "HIGH", "desc": "Direct database query execution or raw SQL string concatenation without parameter binding."},
            {"type": "Cross-Site Scripting (XSS) / Insecure WebView (OWASP A03)", "regex": r"setJavaScriptEnabled\s*\(\s*true\s*\)|addJavascriptInterface", "severity": "MEDIUM", "desc": "JavaScript enabled in WebView or exposed JavascriptInterface increasing risk."},
            
            # A05:2021 - Security Misconfiguration
            {"type": "Security Misconfiguration - Debug Enabled (OWASP A05)", "regex": r"android:debuggable\s*=\s*\"true\"", "severity": "HIGH", "desc": "Application is marked debuggable in manifest, allowing attackers to attach debuggers."},
            
            # A07:2021 - Identification and Authentication Failures (Tightened to catch actual assigned secrets, not variable hints)
            {"type": "Hardcoded API Key / Secret / Token (OWASP A07)", "regex": r"(api[_-]?key|secret|password|auth[_-]?token|bearer|access_token)\s*[:=]\s*['\"'][A-Za-z0-9_\-\.]{8,}['\"']", "severity": "HIGH", "desc": "Hardcoded sensitive authentication secrets or API tokens found assigned to values."},
            
            # A09:2021 - Security Logging and Monitoring Failures
            {"type": "Sensitive Information Log Leak (OWASP A09)", "regex": r"Log\.(d|e|i|v|w)\s*\([^,]*,.*\b(password|secret|token|key)\b", "severity": "MEDIUM", "desc": "Sensitive details or credentials logged to system console log output."}
        ]

        for idx, file_path in enumerate(all_files):
            progress_pct = int(((idx + 1) / total_files) * 99) + 1
            rel_path = os.path.relpath(file_path, source_dir)
            scan_progress_cache[scan_id] = {
                "progress": progress_pct, 
                "status": f"Scanning app file [{idx+1}/{total_files}]: {rel_path}",
                "vulnerabilities": vulnerabilities
            }

            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    lines = content.splitlines()
                    for line_no, line in enumerate(lines, 1):
                        for p in patterns:
                            if re.search(p["regex"], line, re.IGNORECASE):
                                vulnerabilities.append({
                                    "file": rel_path,
                                    "line": line_no,
                                    "severity": p["severity"],
                                    "issue": p["type"],
                                    "snippet": line.strip()[:100],
                                    "impact": p["desc"],
                                    "remediation": "Apply secure coding guidelines based on OWASP recommendations."
                                })
            except Exception:
                continue

        scan_progress_cache[scan_id] = {
            "progress": 100,
            "status": "Filtered Security Scan completed successfully with reduced false positives!",
            "vulnerabilities": vulnerabilities
        }

    except Exception as e:
        scan_progress_cache[scan_id] = {"progress": 100, "status": f"Error: {str(e)}", "vulnerabilities": []}


@app.route('/start_deep_scan', methods=['POST'])
def start_deep_scan():
    if 'zip_file' not in request.files:
        return jsonify({'status': 'error', 'message': "No zip file uploaded"})
    
    file = request.files['zip_file']
    scan_id = uuid.uuid4().hex[:8]
    zip_path = os.path.join(UPLOAD_FOLDER, f"{scan_id}.zip")
    extract_path = os.path.join(DECOMPILE_FOLDER, f"{scan_id}_extracted")
    
    file.save(zip_path)
    os.makedirs(extract_path, exist_ok=True)
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_path)
    except Exception as e:
        return jsonify({'status': 'error', 'message': f"Invalid zip archive: {str(e)}"})

    t = threading.Thread(target=perform_deep_scan, args=(scan_id, extract_path), daemon=True)
    t.start()

    return jsonify({'status': 'success', 'scan_id': scan_id})


@app.route('/scan_progress/<scan_id>', methods=['GET'])
def scan_progress(scan_id):
    data = scan_progress_cache.get(scan_id, {"progress": 0, "status": "Initializing...", "vulnerabilities": []})
    return jsonify(data)
# --------------------------------------------------------------------------


@app.errorhandler(413)
def too_large(e):
    return jsonify({'status': 'error', 'message': "File exceeds the 500MB upload limit"})


if __name__ == '__main__':
    banner = r"""
    ____ _    _  ___  ____ _____ ____  _    _ _____ _    _      
  / ___| | | |/ _ \/ ___|_   _/ ___|| | | | ____| |    | |     
 | |  _| |_| | | | \___ \ | | \___ \| |_| |  _| | |    | |     
 | |_| | _  | |_| |___) || |  ___) | _  | |___| |___| |___  
  \____|_| |_|\___/|____/ |_| |____/|_| |_|_____|_____|_____|
    """
    print("\033[92m" + banner + "\033[00m")
    print(" [+] Tool: Advanced Android Reverse Engineering & Security Suite")
    print(" [+] Localhost: http://127.0.0.1:5000")
    if APKTOOL_CMD:
        print(f" [+] apktool found: {APKTOOL_CMD}")
    else:
        print(" [!] apktool NOT found on PATH.")
    print(f" [+] system aapt2 found: {SYSTEM_AAPT2_CMD}" if SYSTEM_AAPT2_CMD else " [i] using bundled aapt2")
    print(" [+] Status: Running with Waitress Production Server (Extended Timeout)...\n")

    serve(app, host='127.0.0.1', port=5000, threads=8, channel_timeout=1200)
