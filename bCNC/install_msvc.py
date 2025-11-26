import os
import sys
import subprocess
import ctypes
import tempfile

# Try to import requests, but don't crash app if missing
try:
    import requests
except ImportError:
    requests = None


# ---------------------------
# Admin Check
# ---------------------------
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


# ---------------------------
# Locate vswhere.exe
# ---------------------------
def find_vswhere():
    """
    Try to locate vswhere.exe in common locations.
    Returns full path or None if not found.
    """
    candidates = []

    # ProgramFiles(x86)\Microsoft Visual Studio\Installer\vswhere.exe
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates.append(os.path.join(pf86, "Microsoft Visual Studio", "Installer", "vswhere.exe"))

    # Also try ProgramFiles, just in case
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates.append(os.path.join(pf, "Microsoft Visual Studio", "Installer", "vswhere.exe"))

    for path in candidates:
        if os.path.isfile(path):
            return path

    return None


# ---------------------------
# Check MSVC v143 (VC Tools)
# ---------------------------
def msvc_v143_installed():
    """
    Returns True if a VS installation with C/C++ build tools
    (Microsoft.VisualStudio.Component.VC.Tools.x86.x64) is present.
    This corresponds to the MSVC toolset (v143 on VS 2022).
    """
    vswhere_path = find_vswhere()
    if not vswhere_path:
        # No VS installed / no vswhere → assume missing tools
        return False

    try:
        output = subprocess.check_output([
            vswhere_path,
            "-latest",
            "-products", "*",
            "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property", "installationPath"
        ], stderr=subprocess.STDOUT)
        install_path = output.decode(errors="ignore").strip()
        return bool(install_path)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    except Exception:
        # Any weird error → treat as not installed
        return False


# ---------------------------
# Download & Install MSVC v143
# ---------------------------
def install_msvc():
    """
    Downloads and silently installs VS Build Tools with
    C/C++ (VC) toolset. Requires admin.
    """
    if requests is None:
        print("❌ 'requests' module not available. Cannot download MSVC installer.")
        return False

    print("Downloading MSVC Build Tools Installer...")

    url = "https://aka.ms/vs/17/release/vs_BuildTools.exe"
    temp_file = os.path.join(tempfile.gettempdir(), "vs_buildtools.exe")

    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(temp_file, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
    except Exception as e:
        print(f"❌ Failed to download Build Tools installer: {e}")
        return False

    print("Running MSVC Build Tools installer (silent)...")

    try:
        # Add VC tools component
        code = subprocess.call([
            temp_file,
            "--quiet",
            "--wait",
            "--norestart",
            "--add", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"
        ])
        if code != 0:
            print(f"❌ Installer exited with code {code}")
            return False
    except Exception as e:
        print(f"❌ Failed to run installer: {e}")
        return False

    print("✅ MSVC v143 / VC Tools installation completed.")
    return True


# ---------------------------
# Public helper to use from your app
# ---------------------------
def ensure_msvc_v143():
    """
    Call this from your app.
    - Returns True if tools already installed or installed successfully.
    - Returns False if installation failed or user denied admin.
    """
    if msvc_v143_installed():
        print("MSVC v143 already installed.")
        return True

    print("MSVC v143 NOT FOUND.")

    # If not admin → request UAC popup to rerun this script
    if not is_admin():
        print("Requesting admin permission to install MSVC v143...")

        # Re-run the current script with admin ('runas' triggers the UAC dialog)
        params = " ".join(f"\"{arg}\"" for arg in sys.argv[1:])
        ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            f"\"{sys.argv[0]}\" {params}",
            None,
            1
        )
        # Current (non-admin) process stops here
        return False

    # This part runs only in the elevated (admin) process
    ok = install_msvc()
    if not ok:
        return False

    # Re-check after install
    return msvc_v143_installed()


# ---------------------------
# MAIN (for standalone usage)
# ---------------------------
if __name__ == "__main__":
    success = ensure_msvc_v143()
    if success:
        print("✅ Environment ready (MSVC present).")
    else:
        print("⚠ MSVC could not be verified/installed.")
