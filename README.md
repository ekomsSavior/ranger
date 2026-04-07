 # Ranger

**A Malleable, Cross-Platform C2 Framework with WordPress Mimicry & DNS Exfiltration**

Ranger is a command and control framework designed for red team operations, security research, and adversary simulation. It features a three-stage deployment model, advanced evasion techniques, and a modular payload architecture that works across Windows, Linux, macOS, Android, and iOS.

##  Key Features

###  **Multi-Layer Evasion**
- **Stage 1 (Stager)**: Compiled executable with environment keying, sandbox detection, and self-destruct
- **Stage 2 (Implant)**: Memory-only Python execution, B-Tier process masking (taskhostw.exe, metadatah, packagekitd)
- **Stage 3 (C2)**: WordPress-mimicking Nginx proxy with cookie-based data transfer

###  **Cryptographic Trust**
- Ed25519-signed payloads with timestamp/nonce replay protection
- Fernet-encrypted C2 channels with per-implant session keys
- Hardware-anchored fingerprinting (MAC + disk serial)

###  **Multi-Channel Communication**
- **Primary Channel**: HTTPS with cookie-embedded data (mimics web traffic)
- **Secondary Channel**: DNS tunneling for stealthy exfiltration
- **Fallback**: Legacy AES-EAX for backward compatibility

###  **Modular Payload System**
- 30+ pre-built payloads in `/payloads` directory
- Dynamic loading from C2 without recompilation
- Support for custom modules via simple Python interface

###  **Operator Dashboard**
- Real-time implant monitoring with jitter analysis
- DNS exfil viewer with data reconstruction
- Payload management and deployment console

##  Architecture Overview

```markdown

┌─────────────┐     HTTPS      ┌─────────────┐     DNS      ┌─────────────┐
│   STAGER    │ ─────────────> │     C2      │ ───────────> │   CLIENT    │
│ (compiled)  │ <───────────── │   Server    │ <─────────── │   Implant   │
└─────────────┘    Payloads    └─────────────┘   Queries    └─────────────┘
                                    │
                                    ▼
                            ┌─────────────┐
                            │  WordPress  │
                            │  Nginx Mask │
                            └─────────────┘
```

##  Installation

### Prerequisites
```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y nginx python3-pip openssl

# Python packages
pip3 install flask cryptography pycryptodome dnspython
```

### Quick Deploy
```bash
# Clone the repository
git clone https://github.com/ekomsSavior/ranger.git
cd ranger

# Deploy Nginx mask and C2
sudo ./deploy.sh

# Start the C2 server
python3 c2.py

# Access dashboard
# https://your-server:4444/phase1/dashboard
```

## Usage Guide

### 1. Generate a Stager
```bash
# Windows stager
pyinstaller --onefile --noconsole --name svchost.exe stager.py

# Linux stager
pyinstaller --onefile --name packagekitd stager.py

# macOS stager
pyinstaller --onefile --name metadatah stager.py
```

### 2. Deploy Payloads
Place your implant and modules in the `/payloads` directory:
```bash
cp implant.py payloads/
# All .py files in payloads/ are available for dynamic loading
```

### 3. Control Implants via Dashboard
Access `https://your-c2:4444/phase1/dashboard` to:
- View active implants with process names and jitter scores
- Send commands (shell, recon, file operations)
- Enable DNS tunneling for exfiltration
- Monitor exfiltrated data

##  Payload Modules

The framework includes 30+ ready-to-use payloads:

| Category | Payloads |
|----------|----------|
| **Recon** | `sysrecon.py`, `linpeas_light.py`, `cloud_detector.py` |
| **Credential Theft** | `browserstealer.py`, `hashdump.py`, `aws_credential_stealer.py` |
| **Persistence** | `advanced_cron_persistence.py`, `process_inject.py` |
| **Evasion** | `logcleaner.py`, `dnstunnel.py`, `polyloader.py` |
| **Lateral Movement** | `sshspray.py`, `container_escape.py` |
| **Impact** | `fileransom.py`, `ddos.py`, `k8s_secret_stealer.py` |

##  Configuration

### C2 Settings (`c2.py`)
```python
C2_PORT = 4444
C2_HOST = '0.0.0.0'
DNS_DOMAIN = "updates.your-domain.com"
COOKIE_NAMES = ['_ga', '_gid', 'xsid', 'PHPSESSID', 'wordpress_']
```

### Nginx Mask (`nginx/wordpress-mask.conf`)
- Proxies only implant traffic to C2
- Redirects scanners to WordPress.org
- Includes realistic WordPress headers

### Implant Behavior (`implant.py`)
- B-Tier process targeting (taskhostw.exe, sihost.exe, CompatTelRunner.exe)
- Jittered beacon intervals (60-180s with time-based shaping)
- Environmental keying (uptime, RAM, sandbox artifacts)

## Operator Dashboard Features

### Implant Monitoring
- **Jitter Score**: Detects sandboxed implants with perfect timing
- **Process Tracking**: Monitors process name changes
- **DNS Status**: Shows which implants use DNS tunneling

### Command Interface
- **Shell**: Execute system commands
- **Recon**: Gather system information
- **Download/Upload**: File transfer
- **DNS Toggle**: Enable/disable DNS exfiltration

### Exfil Viewer
- Reconstructed data from DNS fragments
- Channel identification (HTTPS vs DNS)
- Timestamp and size metadata

## OpSec Recommendations

### Production Deployment
1. **Use valid SSL certificates** (Let's Encrypt)
2. **Front with CDN** (Cloudflare, AWS CloudFront)
3. **Rotate domains regularly**
4. **Monitor jitter scores** for sandbox detection
5. **Encrypt database** at rest

### Evasion Tips
- **Vary cookie names** (already implemented)
- **Add realistic delays** between DNS queries
- **Mix in legitimate traffic** to your C2 domain
- **Use multiple fallback domains** for DNS tunnel

##  Legal Disclaimer

This software is intended for **authorized security testing and educational purposes only**. Users are responsible for complying with all applicable laws and regulations. The authors assume no liability for misuse or damage caused by this program.
