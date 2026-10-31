<#
.SYNOPSIS
  Registra (o elimina) una tarea programada de Windows que ejecuta InvoiceBot
  de forma desatendida: `invoicebot run --trigger scheduled` cada N minutos.

.DESCRIPTION
  La tarea se crea con el usuario actual y corre mientras su sesión está
  iniciada. Para que corra con la sesión cerrada (servidor o VM dedicada),
  regístrala desde el Programador de tareas con "Ejecutar tanto si el usuario
  inició sesión como si no", o pide a TI una cuenta de servicio.

  Si un ciclo sigue en curso cuando toca el siguiente, Windows no lanza otro
  (MultipleInstances IgnoreNew) e InvoiceBot además lo impide con su candado.

.EXAMPLE
  .\scripts\windows\register-scheduled-task.ps1 -IntervalMinutes 15

.EXAMPLE
  .\scripts\windows\register-scheduled-task.ps1 -IntervalMinutes 15 -WhatIf

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
  [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

if ($Unregister) {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    if ($PSCmdlet.ShouldProcess($TaskName, 'Eliminar la tarea programada')) {
      Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
      Write-Host "Tarea '$TaskName' eliminada."
    }
  } else {
    Write-Host "No existe la tarea '$TaskName'."
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
if (-not (Test-Path $envFile) -or -not (Select-String -Path $envFile -Pattern '^INVOICEBOT_INBOX_DIR=.+' -Quiet)) {
  Write-Warning 'Define INVOICEBOT_INBOX_DIR en .env antes de que corra la tarea; sin carpeta de entrada cada ciclo terminará con error.'
}

$action = New-ScheduledTaskAction `
  -Execute $NodePath `
  -Argument "`"$cli`" run --trigger scheduled" `
  -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger `
  -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet `
  -MultipleInstances IgnoreNew `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$description = "InvoiceBot: procesa los comprobantes de la carpeta de entrada cada $IntervalMinutes minutos. Proyecto: $ProjectDir"

if ($PSCmdlet.ShouldProcess($TaskName, "Registrar la tarea programada cada $IntervalMinutes minutos")) {
  Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description $description `
    -Force | Out-Null
  Write-Host "Tarea '$TaskName' registrada: cada $IntervalMinutes minutos con el usuario actual."
  Write-Host "Probarla ahora:  Start-ScheduledTask -TaskName '$TaskName'"
  Write-Host "Ver resultados:  node `"$cli`" status"
  Write-Host "Eliminarla:      .\scripts\windows\register-scheduled-task.ps1 -Unregister"
}
