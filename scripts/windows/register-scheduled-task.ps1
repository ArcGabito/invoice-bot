<#
.SYNOPSIS
  Registra (o elimina) las tareas programadas de Windows de InvoiceBot:
  `invoicebot run --trigger scheduled` cada N minutos y, con -Serve, la API y la
  pantalla de revisión (`invoicebot serve`) siempre encendidas.

.DESCRIPTION
  Pensado para una VM o un servidor Windows con SQL Server (seguridad integrada).
  Sin -ServiceAccount, las tareas usan el usuario actual y solo corren mientras su
  sesión está iniciada: sirve para probar en un PC.

  En una VM o un servidor usa -ServiceAccount: las tareas corren aunque nadie haya
  iniciado sesión y sobreviven a los reinicios.
  - Cuenta normal (DOMINIO\svc-invoicebot o .\svc-invoicebot): el script pide su
    contraseña una vez; Windows la guarda cifrada.
  - Cuenta de servicio administrada de grupo (gMSA, termina en $): no pide
    contraseña. Es la opción recomendada en un dominio; dale acceso a la base con
    INVOICEBOT_DB_WINDOWS_LOGIN y `invoicebot db init`.

  Si un ciclo sigue en curso cuando toca el siguiente, Windows no lanza otro
  (MultipleInstances IgnoreNew) e InvoiceBot además lo impide con su candado.

.EXAMPLE
  .\scripts\windows\register-scheduled-task.ps1 -IntervalMinutes 15

.EXAMPLE
  .\scripts\windows\register-scheduled-task.ps1 -ServiceAccount 'EMPRESA\gmsa-invoicebot$' -Serve

.EXAMPLE
  .\scripts\windows\register-scheduled-task.ps1 -Unregister
#>
[CmdletBinding(SupportsShouldProcess)]
param(
  [string]$TaskName = 'InvoiceBot',
  [ValidateRange(1, 1440)]
  [int]$IntervalMinutes = 15,
  [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
  [string]$ServiceAccount,
  [switch]$Serve,
  [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$serveTaskName = "$TaskName-API"

if ($Unregister) {
  foreach ($name in @($TaskName, $serveTaskName)) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
      if ($PSCmdlet.ShouldProcess($name, 'Eliminar la tarea programada')) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Tarea '$name' eliminada."
      }
    } else {
      Write-Host "No existe la tarea '$name'."
    }
  }
  return
}

$cli = Join-Path $ProjectDir '.venv\Scripts\invoicebot.exe'
if (-not (Test-Path $cli)) {
  throw "No se encontró $cli. Instala InvoiceBot: py -m venv .venv; .venv\Scripts\pip install -e ."
}
$envFile = Join-Path $ProjectDir '.env'
function Test-EnvSetting([string]$Name) {
  (Test-Path $envFile) -and (Select-String -Path $envFile -Pattern "^$Name=.+" -Quiet)
}
if (-not (Test-EnvSetting 'INVOICEBOT_INBOX_DIR')) {
  Write-Warning 'Define INVOICEBOT_INBOX_DIR en .env: sin carpeta de entrada cada ciclo terminará con error.'
}
if ($Serve -and -not (Test-EnvSetting 'INVOICEBOT_SERVE_HOST')) {
  Write-Warning 'Sin INVOICEBOT_SERVE_HOST=0.0.0.0 en .env, la API solo se podrá usar desde esta máquina.'
}

$registerArgs = @{}
$who = 'el usuario actual (solo con su sesión iniciada)'
if ($ServiceAccount) {
  if ($ServiceAccount.EndsWith('$')) {
    $registerArgs.Principal = New-ScheduledTaskPrincipal -UserId $ServiceAccount -LogonType Password -RunLevel Limited
    $who = "la cuenta administrada $ServiceAccount (sin sesión iniciada)"
  } else {
    $credential = Get-Credential -UserName $ServiceAccount -Message "Contraseña de $ServiceAccount (Windows la guarda cifrada)"
    $registerArgs.User = $credential.UserName
    $registerArgs.Password = $credential.GetNetworkCredential().Password
    $who = "$ServiceAccount (sin sesión iniciada)"
  }
}

function Register-InvoiceBotTask($Name, $Arguments, $Trigger, $Settings, $Description) {
  $action = New-ScheduledTaskAction -Execute $cli -Argument $Arguments -WorkingDirectory $ProjectDir
  Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Settings $Settings `
    -Description $Description -Force @registerArgs | Out-Null
}

if ($PSCmdlet.ShouldProcess($TaskName, "Registrar el ciclo del bot cada $IntervalMinutes minutos con $who")) {
  $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
  $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
  Register-InvoiceBotTask $TaskName 'run --trigger scheduled' $trigger $settings `
    "InvoiceBot: ciclo RPA cada $IntervalMinutes minutos. Proyecto: $ProjectDir"
  Write-Host "Tarea '$TaskName' registrada: cada $IntervalMinutes minutos con $who."
}

if ($Serve -and $PSCmdlet.ShouldProcess($serveTaskName, "Registrar la API al arrancar Windows con $who")) {
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)
  Register-InvoiceBotTask $serveTaskName 'serve' $trigger $settings `
    "InvoiceBot: API REST y pantalla de revisión. Proyecto: $ProjectDir"
  Start-ScheduledTask -TaskName $serveTaskName
  Write-Host "Tarea '$serveTaskName' registrada e iniciada: arranca con Windows con $who."
}

Write-Host ''
Write-Host "Probar el ciclo ahora:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Ver resultados:         `"$cli`" status"
Write-Host "Comprobar todo:         `"$cli`" doctor"
Write-Host 'Eliminar las tareas:    .\scripts\windows\register-scheduled-task.ps1 -Unregister'
