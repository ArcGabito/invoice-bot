<#
.SYNOPSIS
  Crea un certificado HTTPS autofirmado para la pantalla de revisión en red
  (`invoicebot serve` con INVOICEBOT_SERVE_HOST=0.0.0.0).

.DESCRIPTION
  Úsalo si la empresa no tiene una autoridad certificadora interna. Si la
  tiene, pide a TI un certificado para el nombre de esta máquina y usa ese
  .pfx: los navegadores lo aceptarán sin avisos.

  Genera dos archivos en data\tls:
  - revision.pfx: certificado con su clave privada, protegido con contraseña.
    Solo debe poder leerlo la cuenta que ejecuta InvoiceBot.
  - revision.cer: solo la parte pública. Instálalo en los PC de quienes
    revisan (Entidades de certificación raíz de confianza, o por GPO) para que
    el navegador no muestre la advertencia de "conexión no privada".

  Al terminar muestra las líneas que hay que añadir a .env.

.EXAMPLE
  .\scripts\windows\new-review-certificate.ps1

.EXAMPLE
  .\scripts\windows\new-review-certificate.ps1 -DnsName 'invoicebot.empresa.local', 'invoicebot' -Years 2
#>
[CmdletBinding(SupportsShouldProcess)]
param(
  # Nombres con los que se abrirá la pantalla (por defecto, el de esta máquina).
  [string[]]$DnsName = @(
    $env:COMPUTERNAME,
    [System.Net.Dns]::GetHostEntry('').HostName,
    'localhost'
  ),
  [ValidateRange(1, 5)]
  [int]$Years = 2,
  [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
)

$ErrorActionPreference = 'Stop'
$DnsName = $DnsName | Where-Object { $_ } | Select-Object -Unique
$outDir = Join-Path $ProjectDir 'data\tls'
$pfxPath = Join-Path $outDir 'revision.pfx'
$cerPath = Join-Path $outDir 'revision.cer'

if (-not $PSCmdlet.ShouldProcess(($DnsName -join ', '), "Crear un certificado autofirmado válido $Years años")) {
  return
}

New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$cert = New-SelfSignedCertificate `
  -DnsName $DnsName `
  -CertStoreLocation 'Cert:\CurrentUser\My' `
  -FriendlyName 'InvoiceBot - pantalla de revisión' `
  -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 `
  -KeyExportPolicy Exportable `
  -NotAfter (Get-Date).AddYears($Years) `
  -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.1')

# Contraseña aleatoria del .pfx: va a .env, que solo debe leer la cuenta del bot.
$bytes = New-Object byte[] 24
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$passphrase = [Convert]::ToBase64String($bytes) -replace '[+/=]', ''
$secure = ConvertTo-SecureString -String $passphrase -AsPlainText -Force

Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $secure | Out-Null
Export-Certificate -Cert $cert -FilePath $cerPath | Out-Null
# El certificado ya está en los archivos: se quita del almacén del usuario.
Remove-Item -Path "Cert:\CurrentUser\My\$($cert.Thumbprint)"

Write-Host "Certificado creado para: $($DnsName -join ', ') (válido hasta $($cert.NotAfter.ToString('yyyy-MM-dd')))"
Write-Host "  $pfxPath"
Write-Host "  $cerPath  (instálalo en los PC de quienes revisan)"
Write-Host ''
Write-Host 'Añade a .env:'
Write-Host '  INVOICEBOT_SERVE_HOST=0.0.0.0'
Write-Host '  INVOICEBOT_SERVE_TLS_PFX=./data/tls/revision.pfx'
Write-Host "  INVOICEBOT_SERVE_TLS_PASSPHRASE=$passphrase"
