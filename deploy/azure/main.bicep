// InvoiceBot en Azure Container Apps:
// - Container App `api`: API REST y pantalla de revisión (HTTPS administrado).
// - Container Apps Job `cycle`: el ciclo RPA programado (cron), una ejecución a la vez.
// - Azure SQL Database serverless con autenticación Microsoft Entra (identidad administrada,
//   sin contraseñas) y Azure Files para la carpeta de entrada y las exportaciones.
//
//   az group create -n rg-invoicebot -l eastus2
//   az deployment group create -g rg-invoicebot -f deploy/azure/main.bicep \
//     -p deploy/azure/main.parameters.json -p anthropicApiKey=$ANTHROPIC_API_KEY
//
// Guía completa: docs/despliegue-azure.md

targetScope = 'resourceGroup'

@description('Prefijo de los nombres de los recursos (minúsculas y números).')
@minLength(3)
@maxLength(16)
param name string = 'invoicebot'

param location string = resourceGroup().location

@description('Imagen del contenedor (la publica el workflow de GitHub en GHCR).')
param image string = 'ghcr.io/arcgabito/invoice-bot:latest'

@description('Registro privado (vacío si la imagen es pública).')
param registryServer string = ''
param registryUsername string = ''
@secure()
param registryPassword string = ''

@description('Cron del ciclo RPA en UTC (cada 15 minutos por defecto).')
param cycleCron string = '*/15 * * * *'

@description('Réplicas mínimas de la API (0 = escala a cero y arranca en frío).')
@minValue(0)
@maxValue(10)
param apiMinReplicas int = 1

@description('Crear Azure SQL Database serverless. false = usar un SQL Server existente.')
param deploySql bool = true

@description('Servidor SQL existente (solo con deploySql=false), p. ej. miservidor.database.windows.net.')
param existingSqlServer string = ''
param databaseName string = 'invoicebot'

@description('Clave de API de Anthropic (Claude).')
@secure()
param anthropicApiKey string = ''

@description('Proveedor de IA: anthropic | ollama | mock.')
@allowed(['anthropic', 'ollama', 'mock'])
param aiProvider string = 'anthropic'

@description('Variables adicionales (correo, SUNAT, exportación…) como [{name, value}].')
param extraEnv array = []

@description('Secretos adicionales como [{name, value}]; se exponen como variables con el mismo nombre en mayúsculas.')
@secure()
param extraSecrets object = {}

var suffix = uniqueString(resourceGroup().id, name)
var storageName = toLower(take('${replace(name, '-', '')}${suffix}', 24))
var sqlServerName = '${name}-sql-${suffix}'
var shareName = 'invoicebot'
var sqlHost = deploySql ? '${sqlServerName}${environment().suffixes.sqlServerHostname}' : existingSqlServer
var secretEntries = [for item in items(extraSecrets): { name: toLower(replace(item.key, '_', '-')), value: item.value }]

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${name}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-id'
  location: location
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: shareName
  properties: { shareQuota: 100 }
}

resource sqlServer 'Microsoft.Sql/servers@2021-11-01' = if (deploySql) {
  name: sqlServerName
  location: location
  properties: {
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    // Solo Microsoft Entra: la identidad del bot es la administradora; no hay contraseñas.
    administrators: {
      administratorType: 'ActiveDirectory'
      azureADOnlyAuthentication: true
      login: identity.name
      sid: identity.properties.principalId
      tenantId: identity.properties.tenantId
      principalType: 'Application'
    }
  }
}

resource sqlFirewall 'Microsoft.Sql/servers/firewallRules@2021-11-01' = if (deploySql) {
  parent: sqlServer
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource database 'Microsoft.Sql/servers/databases@2021-11-01' = if (deploySql) {
  parent: sqlServer
  name: databaseName
  location: location
  sku: {
    name: 'GP_S_Gen5_1'
    tier: 'GeneralPurpose'
  }
  properties: {
    autoPauseDelay: 60
    minCapacity: json('0.5')
    requestedBackupStorageRedundancy: 'Local'
  }
}

resource environmentResource 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${name}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: environmentResource
  name: 'invoicebot-data'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: shareName
      accessMode: 'ReadWrite'
    }
  }
}

var registries = empty(registryServer) ? [] : [
  {
    server: registryServer
    username: registryUsername
    passwordSecretRef: 'registry-password'
  }
]

var baseSecrets = concat(
  empty(anthropicApiKey) ? [] : [{ name: 'anthropic-api-key', value: anthropicApiKey }],
  empty(registryServer) ? [] : [{ name: 'registry-password', value: registryPassword }],
  secretEntries
)

var env = concat(
  [
    { name: 'INVOICEBOT_DB_SERVER', value: sqlHost }
    { name: 'INVOICEBOT_DB_NAME', value: databaseName }
    { name: 'INVOICEBOT_DB_AUTH', value: 'managed-identity' }
    { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
    { name: 'INVOICEBOT_AI_PROVIDER', value: aiProvider }
    // Un ciclo con facturas observadas es normal: no se marca la ejecución del Job como fallida.
    { name: 'INVOICEBOT_RUN_PARTIAL_EXIT_CODE', value: '0' }
    { name: 'INVOICEBOT_SERVE_COOKIE_SECURE', value: 'true' }
    { name: 'INVOICEBOT_LOG_FORMAT', value: 'json' }
  ],
  empty(anthropicApiKey) ? [] : [{ name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }],
  [for item in items(extraSecrets): { name: toUpper(item.key), secretRef: toLower(replace(item.key, '_', '-')) }],
  extraEnv
)

var volumes = [
  {
    name: 'data'
    storageType: 'AzureFile'
    storageName: envStorage.name
    mountOptions: 'uid=10001,gid=0,dir_mode=0770,file_mode=0660,mfsymlinks,nobrl'
  }
]

var volumeMounts = [{ volumeName: 'data', mountPath: '/data' }]

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-api'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: environmentResource.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: registries
      secrets: baseSecrets
    }
    template: {
      containers: [
        {
          name: 'api'
          image: image
          args: ['serve', '--workers', '2']
          env: env
          resources: { cpu: json('0.5'), memory: '1Gi' }
          volumeMounts: volumeMounts
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8000 }, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/readyz', port: 8000 }, periodSeconds: 15, failureThreshold: 10 }
          ]
        }
      ]
      volumes: volumes
      scale: {
        minReplicas: apiMinReplicas
        maxReplicas: 3
        rules: [{ name: 'http', http: { metadata: { concurrentRequests: '30' } } }]
      }
    }
  }
}

resource cycle 'Microsoft.App/jobs@2024-03-01' = {
  name: '${name}-cycle'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: environmentResource.id
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: cycleCron
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaTimeout: 3600
      // Sin reintentos automáticos: el siguiente ciclo programado retoma lo pendiente.
      replicaRetryLimit: 0
      registries: registries
      secrets: baseSecrets
    }
    template: {
      containers: [
        {
          name: 'cycle'
          image: image
          args: ['run', '--trigger', 'scheduled']
          env: env
          resources: { cpu: json('1.0'), memory: '2Gi' }
          volumeMounts: volumeMounts
        }
      ]
      volumes: volumes
    }
  }
}

output apiUrl string = 'https://${api.properties.configuration.ingress.fqdn}'
output cycleJob string = cycle.name
output sqlServer string = sqlHost
output identityClientId string = identity.properties.clientId
output storageAccount string = storage.name
