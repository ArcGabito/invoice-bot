<#
.SYNOPSIS
  Registra (o elimina) las tareas programadas de Windows de InvoiceBot:
  `invoicebot run --trigger scheduled` cada N minutos y, con -Serve, la
  pantalla de revisión (`invoicebot serve`) siempre encendida.

.DESCRIPTION
  Sin -ServiceAccount, las tareas usan el usuario actual y solo corren
  mientras su sesión está iniciada: sirve para probar en un PC.

  En una VM o un servidor usa -ServiceAccount: las tareas corren aunque nadie
  haya iniciado sesión y sobreviven a los reinicios.
  - Cuenta normal (DOMINIO\svc-invoicebot o .\svc-invoicebot): el script pide
    su contraseña una vez; Windows la guarda cifrada.
  - Cuenta de servicio administrada de grupo (gMSA, termina en $): no pide
    contraseña. Es la opción recomendada en un dominio.
  La cuenta necesita permiso de lectura y escritura en la carpeta del proyecto
  (data, logs, exports) y en la carpeta de entrada, y el derecho "Iniciar
  sesión como proceso por lotes". Registrar con otra cuenta requiere abrir
  PowerShell como administrador.

  Si un ciclo sigue en curso cuando toca el siguiente, Windows no lanza otro
  (MultipleInstances IgnoreNew) e InvoiceBot además lo impide con su candado.

.EXAMPLE
  .\scripts\windows\register-scheduled-task.ps1 -IntervalMinutes 15

.EXAMPLE
  .\scripts\windows\register-scheduled-task.ps1 -ServiceAccount 'EMPRESA\svc-invoicebot' -Serve

.EXAMPLE
  .\scripts\windows\register-scheduled-task.ps1 -ServiceAccount 'EMPRESA\gmsa-invoicebot$' -Serve -WhatIf

.EXAMPLE
  .\scripts\windows\register-scheduled-task.ps1 -Unregister
#>
[CmdletBinding(SupportsShouldProcess)]
param(
  [string]$TaskName = 'InvoiceBot',
  [ValidateRange(1, 1440)]
  [int]$IntervalMinutes = 15,
  [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
  [string]$NodePath = (Get-Command node -ErrorAction SilentlyContinue).Source,
  # Cuenta con la que corren las tareas sin sesión iniciada (VM o servidor).
  [string]$ServiceAccount,
  # Registra además la pantalla de revisión, que arranca con Windows.
  [switch]$Serve,
  [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$serveTaskName = "$TaskName-Revision"

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

if (-not $NodePath) {
  throw 'No se encontró node.exe en el PATH. Instala Node.js 24 LTS o indica -NodePath.'
}
$cli = Join-Path $ProjectDir 'bin\invoicebot.js'
if (-not (Test-Path $cli)) {
  throw "No se encontró $cli. Indica la carpeta del proyecto con -ProjectDir."
}
$envFile = Join-Path $ProjectDir '.env'
function Test-EnvSetting([string]$Name) {
  (Test-Path $envFile) -and (Select-String -Path $envFile -Pattern "^$Name=.+" -Quiet)
}
if (-not (Test-EnvSetting 'INVOICEBOT_INBOX_DIR')) {
  Write-Warning 'Define INVOICEBOT_INBOX_DIR en .env antes de que corra la tarea; sin carpeta de entrada cada ciclo terminará con error.'
}
if ($Serve -and -not (Test-EnvSetting 'INVOICEBOT_SERVE_HOST')) {
  Write-Warning 'Sin INVOICEBOT_SERVE_HOST=0.0.0.0 en .env, la pantalla de revisión solo se podrá usar desde esta máquina.'
}

# Con quién corren las tareas.
$registerArgs = @{}
$who = 'el usuario actual (solo con su sesión iniciada)'
if ($ServiceAccount) {
  if ($ServiceAccount.EndsWith('$')) {
    $registerArgs.Principal = New-ScheduledTaskPrincipal -UserId $ServiceAccount -LogonType Password -RunLevel Limited
    $who = "la cuenta administrada $ServiceAccount (sin sesión iniciada)"
  } else {
    $credential = Get-Credential -UserName $ServiceAccount -Message "Contraseña de $ServiceAccount (Windows la guarda cifrada para las tareas)"
    $registerArgs.User = $credential.UserName
    $registerArgs.Password = $credential.GetNetworkCredential().Password
    $who = "$ServiceAccount (sin sesión iniciada)"
  }
}

function Register-InvoiceBotTask($Name, $Arguments, $Trigger, $Settings, $Description) {
  $action = New-ScheduledTaskAction -Execute $NodePath -Argument "`"$cli`" $Arguments" -WorkingDirectory $ProjectDir
  Register-ScheduledTask `
    -TaskName $Name `
    -Action $action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description $Description `
    -Force @registerArgs | Out-Null
}

if ($PSCmdlet.ShouldProcess($TaskName, "Registrar el ciclo del bot cada $IntervalMinutes minutos con $who")) {
  $trigger = New-ScheduledTaskTrigger `
    -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
  $settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
  Register-InvoiceBotTask $TaskName 'run --trigger scheduled' $trigger $settings `
    "InvoiceBot: procesa los comprobantes de la carpeta de entrada cada $IntervalMinutes minutos. Proyecto: $ProjectDir"
  Write-Host "Tarea '$TaskName' registrada: cada $IntervalMinutes minutos con $who."
}

if ($Serve -and $PSCmdlet.ShouldProcess($serveTaskName, "Registrar la pantalla de revisión al arrancar Windows con $who")) {
  # Arranca con Windows, sin límite de tiempo, y se reinicia si se cae.
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)
  Register-InvoiceBotTask $serveTaskName 'serve' $trigger $settings `
    "InvoiceBot: pantalla de revisión (invoicebot serve). Proyecto: $ProjectDir"
  Start-ScheduledTask -TaskName $serveTaskName
  Write-Host "Tarea '$serveTaskName' registrada e iniciada: arranca con Windows con $who."
}

Write-Host ''
Write-Host "Probar el ciclo ahora:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Ver resultados:         node `"$cli`" status"
Write-Host "Comprobar todo:         node `"$cli`" doctor"
Write-Host 'Eliminar las tareas:    .\scripts\windows\register-scheduled-task.ps1 -Unregister'
