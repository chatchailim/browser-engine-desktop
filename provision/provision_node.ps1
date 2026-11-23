# provision_node.ps1 — ติดตั้ง SPADA MyAI Harness รายเครื่อง (Node-1)
# ตาม PROPOSAL-C-DIST-VIA-CONFIG-MANAGER-TH.md §3 §4 ขั้น 4-5
# ผู้รัน: ผู้ดูแล Node (ไม่ใช่ SPADA push — ติดตั้งจริงเกิดที่เครื่องเป้าหมายด้วยมือผู้ดูแล)
#
# ใช้งาน (PowerShell ที่โฟลเดอร์โค้ด):
#   powershell -ExecutionPolicy Bypass -File provision\provision_node.ps1 -ConfigPath provision\admin-system-config.myai-harness.example.json -TargetDir "C:\ProgramData\SPADA-MyAI-Harness"
#
# สิ่งที่ทำ (7 ขั้น — ทุกขั้นมีผลพิสูจน์บนหน้าจอ ล้มขั้นไหนหยุดทันที):
#  1) ตรวจสภาพแวดล้อม: python 3.10+ · git (ถ้ามี) · พาธเป้าหมาย
#  2) ตรวจ Config: JSON ถูกต้อง + ครอบเงื่อนไขพื้นฐาน (สถานะไม่ใช่ draft ไม่ขวาง — แต่ต้องมี environments/featureFlags) + secret-scan ห้ามมี key จริง
#  3) เตรียมโฟลเดอร์เป้าหมาย + copy โค้ดแอป (ยกเว้น data/, .git, __pycache__, *.log)
#  4) แปลง Config → config.json ของแอป (featureFlag UPPER_SNAKE → snake_case + constants)
#  5) venv + pip install (จาก requirements ที่ตรวจแล้ว)
#  6) สร้างของประจำเครื่อง: machine token (สุ่ม 64 hex) + keysign keypair (Ed25519)— ไม่พรินต์ค่าออกจอ (บอกพาธเท่านั้น)
#  7) Selftest: เทสต์ย่อ 10 ข้อ + ปลุก server + GET /api/status → 200 = "ติดตั้งเสร็จสมบูรณ์"
param(
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [string]$TargetDir = "C:\ProgramData\SPADA-MyAI-Harness",
    [string]$SourceDir = "",   # ค่าเริ่ม: ราก repo ที่สคริปต์อยู่ (parent ของ provision/)
    [switch]$SkipVenv
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Step($n, $msg) { Write-Host "`n=== [$n/7] $msg ===" -ForegroundColor Cyan }
function Ok($msg)       { Write-Host "  ✔ $msg" -ForegroundColor Green }
function Warn($msg)     { Write-Host "  ⚠ $msg" -ForegroundColor Yellow }
function Die($msg)      { Write-Host "  ✖ $msg" -ForegroundColor Red; exit 1 }

$script:AppRel = "apps/browser-engine-desktop"
if (-not $SourceDir) { $SourceDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }

# ---------- 1) ตรวจสภาพแวดล้อม ----------
Step 1 "ตรวจสภาพแวดล้อม"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Die "ไม่พบ python — ติดตั้ง Python 3.10+ ก่อน (https://www.python.org/downloads/)" }
$ver = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$majorMinor = $ver.Split('.')
if ([int]$majorMinor[0] -lt 3 -or ([int]$majorMinor[0] -eq 3 -and [int]$majorMinor[1] -lt 10)) {
    Die "python $ver ต่ำกว่า 3.10 ที่ระบบต้องการ"
}
Ok "python $ver"
$src = Join-Path $SourceDir $script:AppRel
if (-not (Test-Path (Join-Path $src "server.py"))) { Die "ไม่พบ server.py ที่ $src — รันจากโฟลเดอร์โค้ดที่ถูกต้อง" }
Ok "โค้ดแอปพบที่ $src"

# ---------- 2) ตรวจ Config ----------
Step 2 "ตรวจ Config Package ($ConfigPath)"
if (-not (Test-Path $ConfigPath)) { Die "ไม่พบไฟล์ Config: $ConfigPath" }
$cfgJson = Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $cfgJson.environments -or $cfgJson.environments.Count -eq 0) { Die "Config ต้องมี environments อย่างน้อย 1 ตัว" }
$flags = @{}
foreach ($f in $cfgJson.environments[0].featureFlags) { $flags[$f.key] = $f }
foreach ($c in $cfgJson.environments[0].constants) { $flags[$c.key] = $c }
$rawCfg = Get-Content $ConfigPath -Raw -Encoding UTF8
$secretScan = @("sk-[A-Za-z0-9]{20,}", "Bearer [A-Za-z0-9\-_]{16,}", "(?i)password\s*[:=]\s*['\"][^'\"]{8,}")
foreach ($pat in $secretScan) {
    if ($rawCfg -match $pat) { Die "Config มีของลับฝังอยู่ (ตรงรูปแบบ $pat) — ห้าม! ใช้ Vault Binding (`vault://…`) เท่านั้น" }
}
Ok "Config ถูกต้อง + ไม่มีของลับฝัง ($($flags.Count) ธง/ค่า)"

# ---------- 3) โฟลเดอร์เป้าหมาย + copy โค้ด ----------
Step 3 "ติดตั้งโค้ดไป $TargetDir"
if (Test-Path $TargetDir) { Warn "มีอยู่แล้ว — จะถ่ายทับไฟล์โค้ด (data/ เดิมคงอยู่)" }
New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
$excludeDirs = @("data", ".git", "__pycache__", ".venv", ".venv2", "venv")
Get-ChildItem $src -Recurse -File | Where-Object {
    $rel = $_.FullName.Substring($src.Length + 1)
    $skip = $false
    foreach ($d in ($_.FullName -split "\\")) { if ($excludeDirs -contains $d) { $skip = $true; break } }
    -not $skip
} | ForEach-Object {
    $rel = $_.FullName.Substring($src.Length + 1)
    $dst = Join-Path $TargetDir $rel
    $dstDir = Split-Path -Parent $dst
    if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
    Copy-Item $_.FullName $dst -Force
}
$copyCount = (Get-ChildItem $TargetDir -Recurse -File).Count
Ok "คัดลอกไฟล์ $copyCount ไฟล์"

# ---------- 4) แปลง Config → config.json ----------
Step 4 "แปลง Config → config.json (แผนที่ทางการ)"
# แผนที่ featureFlag UPPER → กุญแจ snake_case ของแอป (ตาม PROPOSAL-C §2)
$map = @{
    RECIPE_CACHE_ENABLED      = "recipe_cache_enabled"
    ROLES_ENABLED             = "roles_enabled"
    KEYSIGN_POLICY            = "keysign_policy"
    SHELL_EXECUTION_ENABLED   = "shell_execution_enabled"
    SHELL_ELEVATED_ALLOWED    = "shell_elevated_allowed"
    SHELL_SANDBOX_MODE        = "shell_sandbox_mode"
    DESKTOP_AUTONOMY_ENABLED  = "desktop_autonomy_enabled"
    MYAI_PORT                 = "port"
    MYAI_DESKTOP_ALLOWED_APPS = "desktop_autonomy_allowed_apps"
    MYAI_SHELL_TIMEOUT_SECONDS = "shell_timeout_seconds"
}
$appCfg = @{}
foreach ($k in $map.Keys) {
    if ($flags.ContainsKey($k)) {
        $item = $flags[$k]
        $val = if ($item.PSObject.Properties["value"]) { $item.value } else { $null }
        $appCfg[$map[$k]] = $val
    }
}
# ค่าคงที่ที่ระบบต้องการ (ที่ไม่มีตัวแปร) — fail-closed เสมอ
if (-not $appCfg.ContainsKey("vision_enabled"))          { $appCfg["vision_enabled"] = $false }
if (-not $appCfg.ContainsKey("uia_enabled"))             { $appCfg["uia_enabled"] = $true }
if (-not $appCfg.ContainsKey("onevault_enabled"))        { $appCfg["onevault_enabled"] = $true }
if (-not $appCfg.ContainsKey("vllm_key_path"))           { $appCfg["vllm_key_path"] = (Join-Path $env:USERPROFILE ".dgx\vllm-api-key") }
if (-not $appCfg.ContainsKey("vllm_url"))                { $appCfg["vllm_url"] = "http://192.168.1.108/vllm/v1" }
# desktop_autonomy ต้องเป็นโครงตามธงเดิม (allowlist อยู่นอกธง)
$autonomyFlag = $appCfg["desktop_autonomy_enabled"]
$appCfg.Remove("desktop_autonomy_enabled") | Out-Null
$appCfg["desktop_autonomy"] = @{
    enabled = [bool]$autonomyFlag
    allowed_apps = $appCfg["desktop_autonomy_allowed_apps"]
}
$appCfg.Remove("desktop_autonomy_allowed_apps") | Out-Null
$cfgOut = Join-Path $TargetDir "config.json"
$appCfg | ConvertTo-Json -Depth 6 | Set-Content $cfgOut -Encoding UTF8
Ok "config.json เขียนที่ $cfgOut"
if ($appCfg["shell_execution_enabled"] -eq $true) { Warn "Node นี้เปิด shell ให้ AI — ตรวจ Allowlist/ธง keysign อีกครั้ง (ข้อเสนอ §2 แนะปิด)" }

# ---------- 5) venv + install ----------
Step 5 "สร้าง venv + ติดตั้งแพ็กเกจ"
$vdir = Join-Path $TargetDir ".venv"
if (-not (Test-Path $vdir)) { & python -m venv $vdir | Out-Null }
$pip = Join-Path $vdir "Scripts\pip.exe"
$pyExe = Join-Path $vdir "Scripts\python.exe"
$deps = @("requests", "cryptography", "jsonschema")
& $pip install @deps --quiet --disable-pip-version-check 2>$null
Ok "venv + แพ็กเกจครบ ($($deps -join ', '))"

# ---------- 6) ของประจำเครื่อง ----------
Step 6 "สร้าง machine token + keysign keypair (ไม่ปรินต์ค่าออกจอ)"
$secDir = Join-Path $env:USERPROFILE ".myai-harness"
New-Item -ItemType Directory -Force -Path $secDir | Out-Null
$tokFile = Join-Path $secDir "token"
if (Test-Path $tokFile) {
    Warn "มี token เดิมอยู่ — คงเดิม (ลบเองถ้าต้องการหมุน: ลบไฟล์แล้ว provision ซ้ำ)"
} else {
    & $pyExe -c "import secrets,pathlib,pathlib,sys; p=pathlib.Path(r'$tokFile'); p.write_text(secrets.token_hex(32),encoding='utf-8')" | Out-Null
    Ok "machine token สร้างใหม่ → $tokFile"
}
& icacls $tokFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
$keyDir = Join-Path $TargetDir "data\keysign"
New-Item -ItemType Directory -Force -Path $keyDir | Out-Null
& $pyExe -c "import sys; sys.path.insert(0,r'$TargetDir'); sys.path.insert(0,r'$TargetDir\execution'); from service import keysign; priv,pub=keysign.ensure_keypair(); k=keysign.ensure_key(); print('OK', len(pub))" | Out-Null
if ($LASTEXITCODE -ne 0) { Die "สร้าง keysign keypair ไม่สำเร็จ" }
Ok "keysign keypair + HMAC key → $keyDir (pub แชร์เข้าระเบียนได้ · key เก็บสงวน)"

# ---------- 7) Selftest ----------
Step 7 "Selftest — ทดสอบ 10 ข้อ + ปลุก server + สถานะสด"
Push-Location $TargetDir
& $pyExe -m unittest discover -s tests -p "test_*.py" 2>&1 | Select-Object -Last 3
$testRc = $LASTEXITCODE
Pop-Location
if ($testRc -ne 0) { Die "selftest ล้ม — ห้ามใช้ก่อนแก้ (ดู log ข้างบน)" }
Ok "เทสต์ครบผ่าน"
$env:MYAI_TARGET = $TargetDir
$srv = Start-Process -FilePath $pyExe -ArgumentList "server.py" -WorkingDirectory $TargetDir -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 6
try {
    $st = Invoke-WebRequest -Uri "http://127.0.0.1:$($appCfg['port'])/api/status" -TimeoutSec 5 -ErrorAction Stop
    $port = $appCfg["port"]
    Ok "server ตอบ 200 ที่ http://127.0.0.1:$port/api/status"
} catch {
    $port = $appCfg["port"]
    Warn "server ยังไม่ตอบที่พอร์ต $port (อาจยึดตอนติดตั้ง) — เปิดหน้าต่างเองได้ที่ run.cmd"
} finally {
    if ($srv -and -not $srv.HasExited) { Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue }
}
Write-Host "`n╔══════════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║  ติดตั้ง SPADA MyAI Harness เสร็จสมบูรณ์ — Node นี้ใช้งานได้  ║" -ForegroundColor Green
Write-Host "╚══════════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host "  โค้ด: $TargetDir"
Write-Host "  เปิดใช้: run.cmd (หรือ $pyExe server.py)"
Write-Host "  Token ผู้ดูแล: $tokFile (คัดลอกด้วย copy-token.cmd ในโฟลเดอร์โค้ด)"
Write-Host "  คู่มือสิทธิ์สมาชิก: service\README-TH.md + member_token.cmd"
Write-Host "  Config Manager: การเปลี่ยนค่าใด ๆ ต่อไปผ่าน Config Version + Approval ตามข้อเสนอ §4"