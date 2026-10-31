#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Prepara un SQL Server instalado en este PC para desarrollar y probar InvoiceBot.

.DESCRIPTION
  Cambia la configuración del servidor, por eso lo ejecuta una persona y
  como administrador. Hace cuatro cosas:
  1. Activa TCP/IP en el puerto indicado (1433). El driver de Node (tedious)
     no usa memoria compartida ni canalizaciones con nombre.
  2. Activa la autenticación mixta (Windows y SQL Server), como en el
     contenedor de producción.
  3. Reinicia el servicio de SQL Server.
  4. Crea (o renueva) el inicio de sesión de desarrollo con el rol dbcreator:
     puede crear sus propias bases de datos de prueba, pero no ve las demás.
     Escribe sus datos en .env.test (no se sube a git).

  No abre ningún puerto en el firewall de Windows: el servidor solo se usa
  desde este equipo.

.EXAMPLE
  .\scripts\dev\setup-local-sqlserver.ps1

.EXAMPLE
  .\scripts\dev\setup-local-sqlserver.ps1 -Instance SQLEXPRESS -Port 1434
#>
[CmdletBinding(SupportsShouldProcess)]
param(
  [string]$Instance = 'MSSQLSERVER',
  [ValidateRange(1024, 65535)]
  [int]$Port = 1433,
  [string]$Login = 'invoicebot_dev',
  [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
)

$ErrorActionPreference = 'Stop'

$instanceId = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL').$Instance
if (-not $instanceId) { throw "No existe la instancia de SQL Server '$Instance'." }
$serverKey = "HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\$instanceId\MSSQLServer"
$serviceName = if ($Instance -eq 'MSSQLSERVER') { 'MSSQLSERVER' } else { "MSSQL`$$Instance" }
$sqlcmdServer = if ($Instance -eq 'MSSQLSERVER') { 'localhost' } else { "localhost\$Instance" }

if (-not (Get-Command sqlcmd -ErrorAction SilentlyContinue)) {
  throw 'No se encontró sqlcmd. Instala las herramientas de línea de comandos de SQL Server.'
}

if ($PSCmdlet.ShouldProcess($Instance, "Activar TCP en el puerto $Port y la autenticación mixta, y reiniciar el servicio")) {
  Set-ItemProperty "$serverKey\SuperSocketNetLib\Tcp" -Name Enabled -Value 1
  Set-ItemProperty "$serverKey\SuperSocketNetLib\Tcp\IPAll" -Name TcpPort -Value "$Port"
  Set-ItemProperty "$serverKey\SuperSocketNetLib\Tcp\IPAll" -Name TcpDynamicPorts -Value ''
  # 2 = autenticación mixta (Windows y SQL Server).
  Set-ItemProperty $serverKey -Name LoginMode -Value 2
  Restart-Service -Name $serviceName -Force
  Write-Host "SQL Server ($Instance): TCP en el puerto $Port y autenticación mixta activados."
}

# Contraseña aleatoria que cumple la directiva de complejidad de SQL Server.
$alphabet = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 24
$rng.GetBytes($bytes)
$password = 'Ib-' + (-join ($bytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] })) + '-9a'

if ($PSCmdlet.ShouldProcess($Login, 'Crear o renovar el inicio de sesión de desarrollo con el rol dbcreator')) {
  $query = @"
SET NOCOUNT ON;
IF SUSER_ID(N'$Login') IS NULL
  CREATE LOGIN [$Login] WITH PASSWORD = N'$password', CHECK_POLICY = ON, DEFAULT_DATABASE = [master];
ELSE
  ALTER LOGIN [$Login] WITH PASSWORD = N'$password';
ALTER SERVER ROLE [dbcreator] ADD MEMBER [$Login];
"@
  # El servicio tarda unos segundos en aceptar conexiones tras el reinicio.
  for ($try = 1; $try -le 10; $try++) {
    sqlcmd -S $sqlcmdServer -E -C -b -Q $query | Out-Null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 2
  }
  if ($LASTEXITCODE -ne 0) { throw "No se pudo crear el inicio de sesión $Login." }

  $envFile = Join-Path $ProjectDir '.env.test'
  @(
    '# Generado por scripts/dev/setup-local-sqlserver.ps1 (no se sube a git).',
    '# Las pruebas crean y borran sus propias bases de datos invoicebot_test_*.',
    "INVOICEBOT_TEST_DB_SERVER=localhost",
    "INVOICEBOT_TEST_DB_PORT=$Port",
    "INVOICEBOT_TEST_DB_USER=$Login",
    "INVOICEBOT_TEST_DB_PASSWORD=$password",
    'INVOICEBOT_TEST_DB_TRUST_CERT=true'
  ) | Set-Content -Path $envFile -Encoding utf8
  Write-Host "Inicio de sesión '$Login' listo (rol dbcreator). Datos de conexión en $envFile."
}

Write-Host ''
Write-Host 'Comprueba la conexión:  npm test'
