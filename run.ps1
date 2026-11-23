# PowerShell Launcher for Browser Engine Desktop 0.4
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}
try {
    $Host.UI.RawUI.WindowTitle = "Browser Engine Desktop - Local Co-Pilot"
} catch {}

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  Browser Engine Desktop (Local Portable Edition 0.4)" -ForegroundColor Green
Write-Host "  Connected to DGX Spark AI Brain (Qwen3.8-27B)" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""

$appDir = $PSScriptRoot
Set-Location $appDir

# 1. ตรวจสอบ Python (R3)
$pythonCmd = $null
if (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCmd = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonCmd = "py"
}

if (-not $pythonCmd) {
    Write-Host "================================================================" -ForegroundColor Red
    Write-Host "[!] ข้อผิดพลาด: ไม่พบ Python บนเครื่องคอมพิวเตอร์นี้" -ForegroundColor Red
    Write-Host "================================================================" -ForegroundColor Red
    Write-Host "ระบบต้องการ Python 3.10 ขึ้นไปในการทำงาน:" -ForegroundColor Yellow
    Write-Host "1. ดาวน์โหลดได้จาก https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "2. ระหว่างติดตั้ง กรุณาทำเครื่องหมายถูกที่ 'Add Python to PATH'" -ForegroundColor Yellow
    Write-Host "3. เมื่อติดตั้งเสร็จ ให้เปิดโปรแกรมนี้ใหม่อีกครั้ง" -ForegroundColor Yellow
    Write-Host "================================================================" -ForegroundColor Red
    if (-not [Console]::IsInputRedirected) { Pause }
    exit 1
}

# 2. ตรวจสอบและสร้าง Virtual Environment (.venv) และตรวจสอบความสมบูรณ์ของแพ็กเกจ (R1, R2, R5, WO-013.2 R3/R4)
$venvDir = Join-Path $appDir ".venv"
$venvPy = Join-Path $venvDir "Scripts\python.exe"
$venvPlaywright = Join-Path $venvDir "Scripts\playwright.exe"

$needInstall = $false
if (-not (Test-Path $venvPy)) {
    Write-Host "[*] ตรวจพบการรันครั้งแรก: กำลังสร้างสภาพแวดล้อมจำลอง (.venv)..." -ForegroundColor Cyan
    & $pythonCmd -m venv $venvDir
    if (-not (Test-Path $venvPy)) {
        Write-Host "[!] ผิดพลาด: ไม่สามารถสร้างสภาพแวดล้อม .venv ได้" -ForegroundColor Red
        if (-not [Console]::IsInputRedirected) { Pause }
        exit 1
    }
    $needInstall = $true
} else {
    # R5: ตรวจสอบความสมบูรณ์ของแพ็กเกจใน .venv ถ้าไม่ครบให้ติดตั้งซ่อม
    & $venvPy -c "import playwright, PIL" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[*] ตรวจพบแพ็กเกจใน .venv ไม่ครบถ้วน กำลังดำเนินการซ่อมแซม..." -ForegroundColor Yellow
        $needInstall = $true
    }
}

if ($needInstall) {
    Write-Host "[*] กำลังติดตั้ง/ซ่อมแซมแพ็กเกจที่จำเป็น (playwright, pillow)..." -ForegroundColor Cyan
    Write-Host "    (ขั้นตอนนี้อาจใช้เวลา 1-2 นาทีสำหรับการเปิดครั้งแรกหรือการซ่อมแซม)" -ForegroundColor Gray
    # ใช้ --force-reinstall เพื่อให้แน่ใจว่าไฟล์ที่สูญหายจะถูกเขียนทับกลับมาแม้ dist-info เดิมยังค้างอยู่ (WO-013.2 R3)
    & $venvPy -m pip install --quiet --disable-pip-version-check --force-reinstall playwright pillow
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[!] กำลังลองติดตั้งซ้ำ..." -ForegroundColor Yellow
        & $venvPy -m pip install --force-reinstall playwright pillow
    }

    # ตรวจสอบว่า playwright ติดตั้งสำเร็จก่อนเรียก playwright.exe เพื่อไม่ให้เกิด traceback หากติดตั้งล้มเหลว
    & $venvPy -c "import playwright" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[*] กำลังตรวจสอบและติดตั้ง Chromium สำหรับระบบอัตโนมัติ..." -ForegroundColor Cyan
        & $venvPlaywright install chromium
    }

    # R4: ตรวจสอบซ้ำหลังติดตั้ง/ซ่อมแซม ห้ามสตาร์ตเซิร์ฟเวอร์ถ้ายังไม่ผ่าน (WO-013.2 R4)
    & $venvPy -c "import playwright, PIL" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "================================================================" -ForegroundColor Red
        Write-Host "[!] ข้อผิดพลาด: ไม่สามารถติดตั้งหรือซ่อมแซมแพ็กเกจ (playwright, pillow) ได้สำเร็จ" -ForegroundColor Red
        Write-Host "================================================================" -ForegroundColor Red
        Write-Host "สาเหตุและวิธีแก้ไข:" -ForegroundColor Yellow
        Write-Host "1. แพ็กเกจในสภาพแวดล้อมจำลอง (.venv) อาจเสียหายอย่างรุนแรง หรือระบบเครือข่ายขัดข้อง" -ForegroundColor Yellow
        Write-Host "2. ให้ลบโฟลเดอร์ .venv ทิ้งแล้วเปิดโปรแกรมใหม่:" -ForegroundColor Yellow
        Write-Host "   Remove-Item -Recurse -Force .venv" -ForegroundColor Gray
        Write-Host "3. ตรวจสอบการเชื่อมต่ออินเทอร์เน็ตสำหรับการดาวน์โหลดแพ็กเกจผ่าน pip" -ForegroundColor Yellow
        Write-Host "================================================================" -ForegroundColor Red
        if (-not [Console]::IsInputRedirected) { Pause }
        exit 1
    }

    Write-Host "[*] การเตรียมสภาพแวดล้อมเสร็จสมบูรณ์!" -ForegroundColor Green
    Write-Host ""
}

# 3. ตรวจสอบ Google Chrome / Chromium (R3)
$chromePaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
)
$chromeFound = $chromePaths | Where-Object { Test-Path $_ }
if ($chromeFound) {
    Write-Host "[*] ตรวจพบ Google Chrome ในระบบพร้อมใช้งาน" -ForegroundColor Green
} else {
    Write-Host "[*] หมายเหตุ: ไม่พบ Google Chrome มาตรฐาน ระบบจะใช้ Chromium ภายใน (.venv) แทน" -ForegroundColor Yellow
}

# 4. ตรวจสอบพอร์ต 8205 (R3, K4)
$portInUse = $false
if (Get-NetTCPConnection -LocalPort 8205 -State Listen -ErrorAction SilentlyContinue) {
    $portInUse = $true
} else {
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient("127.0.0.1", 8205)
        $tcp.Close()
        $portInUse = $true
    } catch {
        $portInUse = $false
    }
}

if ($portInUse) {
    Write-Host "================================================================" -ForegroundColor Red
    Write-Host "[!] ข้อผิดพลาด: พอร์ต 8205 ถูกใช้งานอยู่แล้วโดยโปรแกรมอื่น" -ForegroundColor Red
    Write-Host "================================================================" -ForegroundColor Red
    Write-Host "สาเหตุและวิธีแก้ไข:" -ForegroundColor Yellow
    Write-Host "1. มีเซิร์ฟเวอร์ Browser Engine เดิมเปิดค้างอยู่ ให้ปิดหน้าต่างนั้น" -ForegroundColor Yellow
    Write-Host "2. ตรวจสอบและปิดโปรเซสที่ใช้พอร์ตนี้:" -ForegroundColor Yellow
    Write-Host "   Get-NetTCPConnection -LocalPort 8205 | ForEach-Object { Stop-Process -Id `$_.OwningProcess -Force }" -ForegroundColor Gray
    Write-Host "3. หรือเปลี่ยนหมายเลขพอร์ตใน config.json เป็นพอร์ตอื่น เช่น 8206" -ForegroundColor Yellow
    Write-Host "================================================================" -ForegroundColor Red
    if (-not [Console]::IsInputRedirected) { Pause }
    exit 1
}

# 5. ตรวจสอบการเชื่อมต่อ DGX AI Brain (R3, R4, K5)
$configFile = Join-Path $appDir "config.json"
$vllmUrl = "http://192.168.1.108/vllm/v1"
$vllmKeyPath = Join-Path $env:USERPROFILE ".dgx\vllm-api-key"
if (Test-Path $configFile) {
    try {
        $cfgJson = Get-Content $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfgJson.vllm_url) {
            $vllmUrl = $cfgJson.vllm_url.TrimEnd('/')
        }
        if ($cfgJson.vllm_key_path) {
            $vllmKeyPath = $cfgJson.vllm_key_path
        }
    } catch {}
}

$dgxKey = ""
if (Test-Path $vllmKeyPath) {
    try {
        $dgxKey = (Get-Content $vllmKeyPath -Raw -Encoding UTF8).Trim()
    } catch {}
}

$modelsUrl = "$vllmUrl/models"
$dgxOk = $false
try {
    $req = [System.Net.WebRequest]::Create($modelsUrl)
    $req.Timeout = 2000
    if ($dgxKey) {
        $req.Headers.Add("Authorization", "Bearer $dgxKey")
    }
    $resp = $req.GetResponse()
    $httpCode = [int]$resp.StatusCode
    $resp.Close()
    if ($httpCode -ge 200 -and $httpCode -lt 500) {
        $dgxOk = $true
    }
} catch [System.Net.WebException] {
    if ($_.Exception.Response) {
        # ถือว่าการได้รับ HTTP ตอบกลับใด ๆ รวมถึง 401 คือเชื่อมต่อได้ (R4)
        $httpCode = [int]$_.Exception.Response.StatusCode
        if ($httpCode -gt 0) {
            $dgxOk = $true
        }
    } else {
        $dgxOk = $false
    }
} catch {
    $dgxOk = $false
}

if ($dgxOk) {
    Write-Host "[*] เชื่อมต่อ DGX AI Brain ($vllmUrl) สำเร็จ พร้อมใช้งาน" -ForegroundColor Green
} else {
    Write-Host "[!] แจ้งเตือน: ไม่สามารถเชื่อมต่อ DGX AI Brain ($vllmUrl) ได้ในขณะนี้" -ForegroundColor Yellow
    Write-Host "    ระบบจะเปิดแดชบอร์ดในโหมดออฟไลน์/สำรอง (คุณยังสามารถดูสถานะและตรวจสอบระบบได้)" -ForegroundColor Gray
}

# 6. สร้างและตั้งสิทธิ์ Token ให้เฉพาะผู้ใช้ปัจจุบัน (R4)
$tokenDir = Join-Path $env:USERPROFILE ".browser-engine-desktop"
if (-not (Test-Path $tokenDir)) {
    New-Item -ItemType Directory -Path $tokenDir -Force | Out-Null
}
$tokenFile = Join-Path $tokenDir "token"
if (-not (Test-Path $tokenFile)) {
    & $venvPy -c "import secrets, pathlib; pathlib.Path(r'$tokenFile').write_text(secrets.token_hex(32), encoding='utf-8')"
    icacls "$tokenFile" /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    Write-Host "[*] สร้าง Desktop Token ใหม่เรียบร้อยแล้ว (จำกัดสิทธิ์เฉพาะผู้ใช้ปัจจุบัน)" -ForegroundColor Cyan
} else {
    icacls "$tokenFile" /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    Write-Host "[*] ตรวจพบ Desktop Token พร้อมใช้งาน (จำกัดสิทธิ์เฉพาะผู้ใช้ปัจจุบัน)" -ForegroundColor Cyan
}
Write-Host ""

# 7. เปิดเบราว์เซอร์และสตาร์ตเซิร์ฟเวอร์
Write-Host "[*] กำลังเปิด Local Web Dashboard ที่ http://127.0.0.1:8205 ..." -ForegroundColor Yellow
Start-Process "http://127.0.0.1:8205"

& $venvPy server.py
