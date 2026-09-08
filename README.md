# GhostShell - Cyber Security & Utility Suite

GhostShell is an advanced, lightweight cybersecurity and utility suite built with Python and Flask. It features powerful offensive and analytical tools wrapped in a clean, dark cyber-hacker aesthetic UI.

---

## Core Features & Workflow

The suite provides a structured pipeline for mobile application security assessments, divided into three main operational stages:

1. **APK Decompilation**
   * Upload target Android APK files (up to 500MB) directly through the web interface.
   * Automated backend processing extracts source code (Smali/DEX), resources, and AndroidManifest files into a structured directory for deep analysis.

2. **Source Inspection and Editing**
   * Access the extracted application files to review business logic, hardcoded credentials, API endpoints, and insecure configurations.
   * Modify resource files, configurations, or inject security patches directly within the development environment before rebuilding.

3. **APK Recompilation and Vulnerability Scanning**
   * Recompile modified source files and resources back into a functional APK package.
   * Built-in security setup supports vulnerability scanning workflows to identify mobile-specific flaws such as insecure data storage, weak cryptographic implementations, improper component exports, and hazardous interface bindings.

---

## Technical Stack

* **Backend:** Python, Flask, Waitress
* **Frontend:** HTML5, CSS3, JavaScript (AJAX / XMLHttpRequest)
* **Security Integration:** APKTool pipeline and automated parsing utilities

---

## Installation & Setup

Follow these steps to run GhostShell locally on your machine:

USE THE LOCALHOST IN CHROME

1. **Clone the Repository:**
   ```bash
   git clone https://github.com/ghostprotocolsha/ghostshell.git
   cd ghostshell
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   python app.py
