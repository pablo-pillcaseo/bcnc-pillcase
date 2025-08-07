# bCNC Setup and Run Guide

## **Complete Process (3 Simple Steps)**

### **Step 1: Clone Repository**
```batch
git clone https://github.com/pablo-pillcaseo/bcnc-pillcase
```

**What this does:**
- Downloads the complete bCNC project from GitHub
- Creates a `bcnc-pillcase` folder with all source code

---

### **Step 2: Open Folder and Run Setup Script**
1. **Open the `bcnc-pillcase` folder** in Windows Explorer
2. **Double-click** on `update_and_setup.bat` to run it

*Alternative (Command Line):*
```batch
cd bcnc-pillcase
update_and_setup.bat
```

**What this script does automatically:**
- ✅ **Git Operations**: Fetches latest updates, discards local changes
- ✅ **Python Check**: Verifies Python 3.11 is installed
- ✅ **Auto-Install Python**: Installs Python 3.11 if missing (using winget)
- ✅ **Virtual Environment**: Creates `env` folder with isolated Python environment
- ✅ **Dependencies**: Installs all required packages from `requirements.txt`
- ✅ **Environment Setup**: Configures everything needed to run bCNC

**Expected Output:**
```
===============================
Checking for Git and updating code...
===============================
✅ Git repository found.
Fetching latest changes from remote...
✅ Successfully fetched latest changes.
Pulling latest changes...
✅ Successfully updated to latest code.

===============================
Checking for Python 3.11
===============================
Found Python version: Python 3.11.x

===============================
Creating virtual environment...
===============================
===============================
Activating virtual environment...
===============================
===============================
Installing requirements...
===============================
✅ Environment setup complete!
```

---

### **Step 3: Run bCNC**
1. **In the same folder**, double-click on `run_bCNC.bat` to launch bCNC

*Alternative (Command Line):*
```batch
run_bCNC.bat
```

**What this does:**
- Activates the virtual environment
- Launches the bCNC application
- Opens the bCNC GUI interface

---

### **Step 4: Update bCNC (When Needed)**
1. **In the same folder**, double-click on `update_and_setup.bat` to get latest updates

*Alternative (Command Line):*
```batch
update_and_setup.bat
```

**What this does:**
- Fetches latest code from GitHub
- Updates to newest version
- Reinstalls dependencies if needed
- Keeps your environment ready



---

## **What Each Script Does**

### **`update_and_setup.bat`**
```batch
# This script handles:
1. Git repository management (fetch & update latest code)
2. Python installation (if needed)
3. Virtual environment creation
4. Dependency installation
5. Environment configuration

# Can be used for:
- Initial setup (first time)
- Updates (any time to get latest version)
```

### **`run_bCNC.bat`**
```batch
# This script should contain:
@echo off
call env\Scripts\activate.bat
python bCNC\bCNC.py
pause
```

---

## **Complete Command Sequence**

```batch
REM Step 1: Clone repository
git clone https://github.com/pablo-pillcaseo/bcnc-pillcase
cd bcnc-pillcase

REM Step 2: Setup environment
update_and_setup.bat

REM Step 3: Run bCNC
run_bCNC.bat
```

---

## **Troubleshooting**

### **If Step 1 Fails (Git Clone):**
```batch
# Check if Git is installed
git --version

# If not installed, download from:
# https://git-scm.com/download/win
```

### **If Step 2 Fails (Setup):**
```batch
# Common issues and solutions:

# 1. Python not found
# - Script will auto-install Python 3.11
# - If auto-install fails, manually install from python.org

# 2. Network issues during pip install
# - Check internet connection
# - Try running as administrator

# 3. Permission errors
# - Run Command Prompt as Administrator
# - Or change to a directory with write permissions
```

### **If Step 3 Fails (Run):**
```batch
# Check if environment is properly activated
env\Scripts\activate.bat
python --version

# Should show Python from the env folder
# If not, re-run setup script
```

---

## **Updating bCNC (Future Runs)**

### **To get latest updates:**
**Option 1: Double-click method**
1. Open the `bcnc-pillcase` folder in Windows Explorer
2. Double-click `update_and_setup.bat` to update

**Option 2: Command line**
```batch
update_and_setup.bat
```

**What happens during update:**
- ✅ Fetches latest code from GitHub
- ✅ Updates to newest version
- ✅ Reinstalls dependencies if needed
- ✅ Keeps your environment ready

**Note:** The same `update_and_setup.bat` script works for both initial setup and future updates!

---

## **Manual Commands (If Scripts Don't Work)**

### **Manual Setup:**
```batch
REM Create virtual environment
python -m venv env

REM Activate environment
env\Scripts\activate.bat

REM Install dependencies
pip install -r bCNC\requirements.txt
```

### **Manual Run:**
```batch
REM Activate environment
env\Scripts\activate.bat

REM Run bCNC
python bCNC\bCNC.py
```

---

## **Summary**

**The 4-step process is designed to be simple and automated:**

1. **Clone** → Gets the code
2. **Setup** → Prepares the environment  
3. **Run** → Launches the application
4. **Update** → Gets latest version (when needed)

**Each step handles all the complexity automatically, so you just need to run the commands in order!**