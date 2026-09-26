{{- define "sda.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "sda.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "sda.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "sda.labels" -}}
app.kubernetes.io/name: {{ include "sda.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "sda.backendImage" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}

{{- define "sda.frontendImage" -}}
{{- printf "%s:%s" .Values.frontendImage.repository (default .Chart.AppVersion .Values.frontendImage.tag) -}}
{{- end -}}

{{- define "sda.secretName" -}}
{{- default (printf "%s-secrets" (include "sda.fullname" .)) .Values.existingSecret -}}
{{- end -}}

{{/* Every backend pod gets the same config and secret env, so the API and the worker
     cannot drift apart on a connection string. */}}
{{- define "sda.backendEnv" -}}
envFrom:
  - configMapRef:
      name: {{ include "sda.fullname" . }}-config
  - secretRef:
      name: {{ include "sda.secretName" . }}
{{- end -}}

{{- define "sda.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "sda.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}
