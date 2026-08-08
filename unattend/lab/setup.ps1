# First-boot configuration for the Falcon lab Windows VM.
# Runs once, from FirstLogonCommands in autounattend.xml.
#
# Order matters: the static address goes on first, because everything after it needs the
# network. There is no DHCP on vmbr1 by design.

$ErrorActionPreference = 'Continue'
Start-Transcript -Path 'C:\lab-setup.log' -Force | Out-Null
Write-Output "=== falcon lab setup $(Get-Date -Format o) ==="

# --- static address ---------------------------------------------------------------
$ad = Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' } | Select-Object -First 1
if (-not $ad) { $ad = Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Select-Object -First 1 }

if ($ad) {
    Write-Output "adapter: $($ad.Name) ifIndex=$($ad.ifIndex)"
    Remove-NetIPAddress -InterfaceIndex $ad.ifIndex -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue
    Remove-NetRoute     -InterfaceIndex $ad.ifIndex -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue
    New-NetIPAddress -InterfaceIndex $ad.ifIndex -IPAddress 10.77.0.10 -PrefixLength 24  -DefaultGateway 10.77.0.1 -ErrorAction SilentlyContinue | Out-Null
    Set-DnsClientServerAddress -InterfaceIndex $ad.ifIndex -ServerAddresses '1.1.1.1','9.9.9.9'
    Write-Output "address: $((Get-NetIPAddress -InterfaceIndex $ad.ifIndex -AddressFamily IPv4).IPAddress)"
} else {
    Write-Output 'NO NETWORK ADAPTER FOUND -- is the NetKVM driver present?'
}

# --- remote desktop ---------------------------------------------------------------
Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server'  -Name fDenyTSConnections -Value 0 -ErrorAction SilentlyContinue
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue
Write-Output 'rdp: enabled'

# --- OpenSSH server ---------------------------------------------------------------
# Needs the network, hence the ordering above.
try {
    Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' -ErrorAction Stop | Out-Null
    Set-Service -Name sshd -StartupType Automatic
    Start-Service sshd
    if (-not (Get-NetFirewallRule -Name 'sshd-lab' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'sshd-lab' -DisplayName 'OpenSSH Server (lab)'  -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    }
    Write-Output 'sshd: installed and running'
} catch {
    Write-Output "sshd: FAILED -- $($_.Exception.Message)"
}

# --- authorised key -----------------------------------------------------------------
# labadmin is an administrator, so Windows OpenSSH ignores the per-user file entirely and
# reads administrators_authorized_keys instead -- and silently refuses it unless the ACLs
# are locked to SYSTEM and Administrators. Both halves are required.
$key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGgasoyLXaV5/yYCoQsdhifKvKY5MtFChtBztbnHt8Xk falcon-lab'
$akf = 'C:\ProgramData\ssh\administrators_authorized_keys'
New-Item -ItemType Directory -Force -Path 'C:\ProgramData\ssh' | Out-Null
Set-Content -Path $akf -Value $key -Encoding ascii
icacls $akf /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
Write-Output "authorized_keys: written to $akf"

# PowerShell rather than cmd for incoming SSH sessions.
New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell  -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'  -PropertyType String -Force | Out-Null

# --- quality of life for a lab box --------------------------------------------------
# Long file paths and script execution, both of which Atomic Red Team wants.
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'  -Name LongPathsEnabled -Value 1 -ErrorAction SilentlyContinue
Set-ExecutionPolicy -Scope LocalMachine -ExecutionPolicy RemoteSigned -Force -ErrorAction SilentlyContinue

# Marker the orchestrator polls for to know the box is ready.
"ready $(Get-Date -Format o)" | Out-File -FilePath 'C:\lab-ready.txt' -Encoding ascii
Write-Output '=== setup complete ==='
Stop-Transcript | Out-Null
