// InvoiceBot en Azure Container Apps.
//
// Modo `queue` (por defecto): colas en Azure Managed Redis.
// - Container App `api`: API REST y pantalla de revisión (HTTPS administrado).
// - Container Apps `worker` (ingest, sunat, default) y `extractor` (OCR e IA): escalan de 0 a N
//   con KEDA según la longitud de su lista en Redis.
// - Container Apps Job `scheduler`: `invoicebot schedule tick` cada pocos minutos (encola las
//   tareas programadas y los reintentos vencidos).
// - Container Apps Job `migrate`: `invoicebot db migrate`, manual, antes de cada versión.
//
// Modo `cycle` (clásico, sin Redis): Container Apps Job `cycle` con `invoicebot run`.
//
// En los dos: Azure SQL Database serverless con autenticación Microsoft Entra (identidad
// administrada, sin contraseñas) y Azure Files para la carpeta de entrada y las exportaciones.
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

@description('queue: API + workers + Redis (escala) · cycle: un Job con el ciclo completo (sin Redis).')
@allowed(['queue', 'cycle'])
param mode string = 'queue'

@description('Cron del ciclo RPA en UTC (solo modo cycle).')
param cycleCron string = '*/15 * * * *'

@description('Cron del programador en UTC (modo queue): encola tareas y reintentos vencidos.')
param schedulerCron string = '*/2 * * * *'

@description('Crear Azure Managed Redis. false = usar redisUrl (otro Redis).')
param deployRedis bool = true

@description('SKU de Azure Managed Redis (Balanced_B0 alcanza para la mayoría de oficinas).')
param redisSku string = 'Balanced_B0'

@description('Redis existente (solo con deployRedis=false): rediss://:clave@host:puerto/0')
@secure()
param redisUrl string = ''

@description('Réplicas máximas de los workers.')
@minValue(1)
@maxValue(30)
param workerMaxReplicas int = 5

@description('Mensajes en cola por réplica del extractor antes de sumar otra.')
param extractorQueueLength int = 5

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

var queueMode = mode == 'queue'
var useManagedRedis = queueMode && deployRedis
var suffix = uniqueString(resourceGroup().id, name)
var storageName = toLower(take('${replace(name, '-', '')}${suffix}', 24))
var sqlServerName = '${name}-sql-${suffix}'
var shareName = 'invoicebot'
var sqlHost = deploySql ? '${sqlServerName}${environment().suffixes.sqlServerHostname}' : existingSqlServer
var secretEntries = [for item in items(extraSecrets): { name: toLower(replace(item.key, '_', '-')), value: item.value }]
var secretEnv = [for item in items(extraSecrets): { name: toUpper(item.key), secretRef: toLower(replace(item.key, '_', '-')) }]

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

// Azure Managed Redis: TLS en el puerto 10000, sin desalojo (perder una clave = perder un
// trabajo) y con AOF. Política de clúster Enterprise: el cliente lo ve como un Redis normal.
resource redis 'Microsoft.Cache/redisEnterprise@2025-04-01' = if (useManagedRedis) {
  name: '${name}-redis-${suffix}'
  location: location
  sku: { name: redisSku }
  properties: {
    minimumTlsVersion: '1.2'
    highAvailability: 'Enabled'
  }
}

resource redisDb 'Microsoft.Cache/redisEnterprise/databases@2025-04-01' = if (useManagedRedis) {
  parent: redis
  name: 'default'
  properties: {
    clientProtocol: 'Encrypted'
    port: 10000
    clusteringPolicy: 'EnterpriseCluster'
    evictionPolicy: 'NoEviction'
    accessKeysAuthentication: 'Enabled'
    persistence: { aofEnabled: true, aofFrequency: '1s' }
  }
}

var redisHost = useManagedRedis ? '${redis!.properties.hostName}:10000' : ''
var redisPassword = useManagedRedis ? redisDb!.listKeys().primaryKey : ''
var effectiveRedisUrl = useManagedRedis ? 'rediss://:${redisPassword}@${redisHost}/0' : redisUrl

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
  queueMode ? [{ name: 'redis-url', value: effectiveRedisUrl }] : [],
  useManagedRedis ? [{ name: 'redis-password', value: redisPassword }] : [],
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
  queueMode
    ? [
        { name: 'INVOICEBOT_QUEUE_BACKEND', value: 'redis' }
        { name: 'INVOICEBOT_REDIS_URL', secretRef: 'redis-url' }
        // Migra solo el Job `migrate`: con varias réplicas no compiten por el esquema.
        { name: 'INVOICEBOT_AUTO_MIGRATE', value: 'false' }
      ]
    : [],
  secretEnv,
  extraEnv
)

// KEDA: una regla por lista de Redis que atiende cada Container App (gana la que pida más).
// Con Azure Managed Redis la dirección y la clave se conocen aquí; con un Redis externo,
// define las reglas a mano o usa minReplicas >= 1.
func queueRule(queue string, length string, host string) object => {
  name: 'queue-${queue}'
  custom: {
    type: 'redis'
    metadata: {
      address: host
      listName: 'invoicebot:${queue}'
      listLength: length
      enableTLS: 'true'
    }
    auth: [{ secretRef: 'redis-password', triggerParameter: 'password' }]
  }
}

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

var workerApps = [
  { name: 'worker', args: ['worker', '--queues', 'ingest,sunat,default'], queues: ['ingest', 'sunat', 'default'], length: '10', cpu: '0.5', memory: '1Gi' }
  { name: 'extractor', args: ['worker', '--queues', 'extract'], queues: ['extract'], length: string(extractorQueueLength), cpu: '1.0', memory: '2Gi' }
]

resource workers 'Microsoft.App/containerApps@2024-03-01' = [for app in workerApps: if (queueMode) {
  name: '${name}-${app.name}'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: environmentResource.id
    configuration: {
      registries: registries
      secrets: baseSecrets
    }
    template: {
      containers: [
        {
          name: app.name
          image: image
          args: app.args
          env: env
          resources: { cpu: json(app.cpu), memory: app.memory }
          volumeMounts: volumeMounts
        }
      ]
      volumes: volumes
      // Al reducir réplicas, Container Apps envía SIGTERM: el worker termina lo que tiene en curso.
      terminationGracePeriodSeconds: 120
      scale: {
        minReplicas: useManagedRedis ? 0 : 1
        maxReplicas: workerMaxReplicas
        rules: useManagedRedis ? map(app.queues, q => queueRule(q, app.length, redisHost)) : []
      }
    }
  }
}]

resource scheduler 'Microsoft.App/jobs@2024-03-01' = if (queueMode) {
  name: '${name}-scheduler'
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
        cronExpression: schedulerCron
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaTimeout: 300
      replicaRetryLimit: 0
      registries: registries
      secrets: baseSecrets
    }
    template: {
      containers: [
        {
          name: 'scheduler'
          image: image
          args: ['schedule', 'tick']
          env: env
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          volumeMounts: volumeMounts
        }
      ]
      volumes: volumes
    }
  }
}

resource migrate 'Microsoft.App/jobs@2024-03-01' = {
  name: '${name}-migrate'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: environmentResource.id
    configuration: {
      triggerType: 'Manual'
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      replicaTimeout: 1800
      replicaRetryLimit: 1
      registries: registries
      secrets: baseSecrets
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: image
          args: ['db', 'migrate']
          env: concat(env, [{ name: 'INVOICEBOT_AUTO_MIGRATE', value: 'true' }])
          resources: { cpu: json('0.5'), memory: '1Gi' }
          volumeMounts: volumeMounts
        }
      ]
      volumes: volumes
    }
  }
}

resource cycle 'Microsoft.App/jobs@2024-03-01' = if (!queueMode) {
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
output mode string = mode
output migrateJob string = migrate.name
output schedulerJob string = queueMode ? scheduler.name : ''
output cycleJob string = queueMode ? '' : cycle.name
output redisHost string = redisHost
output sqlServer string = sqlHost
output identityClientId string = identity.properties.clientId
output storageAccount string = storage.name
