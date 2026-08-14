{{/* Name helpers — the usual shape, nothing surprising. */}}

{{- define "cookiebot.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cookiebot.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "cookiebot.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: cookiebot
{{- end -}}

{{- define "cookiebot.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "cookiebot.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Image for one deployable. The tag defaults to appVersion so a chart pinned in
Git always names a concrete build — `latest` is never reachable from here.
*/}}
{{- define "cookiebot.image" -}}
{{- $root := index . 0 -}}
{{- $service := index . 1 -}}
{{- $tag := default $root.Chart.AppVersion $root.Values.image.tag -}}
{{- printf "%s/%s/%s:%s" $root.Values.image.registry $root.Values.image.repository $service.name $tag -}}
{{- end -}}

{{/* In-cluster DSNs. Explicit externals win; otherwise point at what we deploy. */}}

{{- define "cookiebot.pgDsn" -}}
{{- if .Values.citus.externalDsn -}}
{{- .Values.citus.externalDsn -}}
{{- else -}}
{{- printf "postgresql://%s-citus-rw.%s.svc:5432/%s" (include "cookiebot.fullname" .) .Release.Namespace .Values.citus.database -}}
{{- end -}}
{{- end -}}

{{- define "cookiebot.redisDsn" -}}
{{- if .Values.valkey.externalDsn -}}
{{- .Values.valkey.externalDsn -}}
{{- else -}}
{{- printf "redis://%s-valkey.%s.svc:%d/0" (include "cookiebot.fullname" .) .Release.Namespace (int .Values.valkey.port) -}}
{{- end -}}
{{- end -}}

{{- define "cookiebot.telegramApiBase" -}}
{{- if .Values.telegramBotApi.enabled -}}
{{- printf "http://%s-telegram-bot-api.%s.svc:%d" (include "cookiebot.fullname" .) .Release.Namespace (int .Values.telegramBotApi.port) -}}
{{- end -}}
{{- end -}}

{{/*
Where the gateway tells Telegram to deliver updates.

With the bundled Bot API server this is a ClusterIP address — legal only
because the server runs with --local, and the reason UAT needs no public
hostname at all. An explicit config.webhookBaseUrl overrides it, which is what
a real public deployment sets.
*/}}
{{- define "cookiebot.webhookBaseUrl" -}}
{{- if .Values.config.webhookBaseUrl -}}
{{- .Values.config.webhookBaseUrl -}}
{{- else if and .Values.telegramBotApi.enabled (eq .Values.config.telegramIngest "webhook") -}}
{{- printf "http://%s-cb-gateway.%s.svc:%d" (include "cookiebot.fullname" .) .Release.Namespace (int .Values.services.gateway.port) -}}
{{- end -}}
{{- end -}}

{{/*
The environment every service shares: config from the ConfigMap, credentials
from the Secret. Secret keys that are not configured are simply not injected —
an empty optional key would shadow the default in cb_core/settings.py.
*/}}
{{- define "cookiebot.commonEnv" -}}
envFrom:
  - configMapRef:
      name: {{ include "cookiebot.fullname" . }}-env
env:
  - name: CB_BOT_TOKENS
    valueFrom:
      secretKeyRef:
        name: {{ .Values.secrets.name }}
        key: bot-tokens
  - name: CB_WEBHOOK_SECRET
    valueFrom:
      secretKeyRef:
        name: {{ .Values.secrets.name }}
        key: webhook-secret
        optional: true
{{- if .Values.citus.enabled }}
  {{- /* CloudNativePG owns this Secret; it holds the generated password. */}}
  - name: CB_PG_DSN
    valueFrom:
      secretKeyRef:
        name: {{ include "cookiebot.fullname" . }}-citus-app
        key: uri
{{- else if .Values.citus.externalDsnSecret }}
  - name: CB_PG_DSN
    valueFrom:
      secretKeyRef:
        name: {{ .Values.citus.externalDsnSecret.name }}
        key: {{ .Values.citus.externalDsnSecret.key }}
{{- end }}
{{- /* citus.externalDsn (plain, no credentials) comes through the ConfigMap. */}}
{{- if .Values.secrets.anthropicApiKeyKey }}
  - name: CB_ANTHROPIC_API_KEY
    valueFrom:
      secretKeyRef:
        name: {{ .Values.secrets.name }}
        key: {{ .Values.secrets.anthropicApiKeyKey }}
{{- end }}
{{- if .Values.secrets.openaiApiKeyKey }}
  - name: CB_OPENAI_API_KEY
    valueFrom:
      secretKeyRef:
        name: {{ .Values.secrets.name }}
        key: {{ .Values.secrets.openaiApiKeyKey }}
{{- end }}
{{- if .Values.secrets.googleSearchApiKeyKey }}
  - name: CB_GOOGLE_SEARCH_API_KEY
    valueFrom:
      secretKeyRef:
        name: {{ .Values.secrets.name }}
        key: {{ .Values.secrets.googleSearchApiKeyKey }}
{{- end }}
{{- if .Values.secrets.googleSearchCxKey }}
  - name: CB_GOOGLE_SEARCH_CX
    valueFrom:
      secretKeyRef:
        name: {{ .Values.secrets.name }}
        key: {{ .Values.secrets.googleSearchCxKey }}
{{- end }}
{{- if .Values.objectStorage.enabled }}
{{- /*
     AWS_*, not CB_*: obstore's S3Store reads the ambient AWS environment the
     way every S3 client does, so nothing in cb_core has to learn what a MinIO
     endpoint is. AWS_ALLOW_HTTP is the one that is easy to miss — without it
     the Rust layer refuses a plaintext endpoint and the error names TLS, not
     the scheme.
*/}}
  - name: AWS_ENDPOINT_URL
    value: {{ include "cookiebot.objectStorageEndpoint" . | quote }}
  - name: AWS_ALLOW_HTTP
    value: "true"
  - name: AWS_REGION
    value: {{ .Values.objectStorage.region | quote }}
  - name: AWS_ACCESS_KEY_ID
    valueFrom:
      secretKeyRef:
        name: {{ .Values.objectStorage.credentialsSecret.name }}
        key: {{ .Values.objectStorage.credentialsSecret.accessKeyKey }}
  - name: AWS_SECRET_ACCESS_KEY
    valueFrom:
      secretKeyRef:
        name: {{ .Values.objectStorage.credentialsSecret.name }}
        key: {{ .Values.objectStorage.credentialsSecret.secretKeyKey }}
{{- end }}
{{- end -}}

{{- define "cookiebot.objectStorageEndpoint" -}}
{{- printf "http://%s-objectstore.%s.svc:%d" (include "cookiebot.fullname" .) .Release.Namespace (int .Values.objectStorage.port) -}}
{{- end -}}

{{/*
Where blobs go.

An explicit config.storageUri always wins; the bundled MinIO only fills in the
default. That ordering is what lets one values file enable the bucket without
also having to repeat its name in a URI, while a production values file that
names a real bucket is never second-guessed.
*/}}
{{- define "cookiebot.storageUri" -}}
{{- if and .Values.objectStorage.enabled (eq .Values.config.storageUri "memory://") -}}
{{- printf "s3://%s" .Values.objectStorage.bucket -}}
{{- else -}}
{{- .Values.config.storageUri -}}
{{- end -}}
{{- end -}}
