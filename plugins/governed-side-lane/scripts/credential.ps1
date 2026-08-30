param(
    [Parameter(Position=0, Mandatory=$true)]
    [ValidateSet("set", "delete")]
    [string]$Mode,
    [Parameter(Position=1, Mandatory=$true)]
    [ValidateSet("governed-side-lane-glm")]
    [string]$Service
)

$signature = @"
using System;
using System.Runtime.InteropServices;
public static class SideLaneCredentials {
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct Credential {
    public UInt32 Flags, Type; public string TargetName, Comment;
    public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
    public UInt32 CredentialBlobSize; public IntPtr CredentialBlob;
    public UInt32 Persist, AttributeCount; public IntPtr Attributes;
    public string TargetAlias, UserName;
  }
  [DllImport("advapi32", EntryPoint="CredWriteW", CharSet=CharSet.Unicode, SetLastError=true)]
  public static extern bool CredWrite(ref Credential credential, UInt32 flags);
  [DllImport("advapi32", EntryPoint="CredDeleteW", CharSet=CharSet.Unicode, SetLastError=true)]
  public static extern bool CredDelete(string target, UInt32 type, UInt32 flags);
}
"@
Add-Type -TypeDefinition $signature
if ($Mode -eq "delete") {
    if (-not [SideLaneCredentials]::CredDelete($Service, 1, 0)) { throw "Credential deletion failed" }
    Write-Output "Credential deleted"
    exit 0
}
$secure = Read-Host "Provider key" -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToCoTaskMemUnicode($secure)
try {
    $length = 0
    while ([Runtime.InteropServices.Marshal]::ReadInt16($pointer, $length) -ne 0) { $length += 2 }
    $item = New-Object SideLaneCredentials+Credential
    $item.Type = 1
    $item.TargetName = $Service
    $item.CredentialBlobSize = $length
    $item.CredentialBlob = $pointer
    $item.Persist = 2
    $item.UserName = $env:USERNAME
    if (-not [SideLaneCredentials]::CredWrite([ref]$item, 0)) { throw "Credential write failed" }
    Write-Output "Credential stored for current user"
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeCoTaskMemUnicode($pointer)
}
