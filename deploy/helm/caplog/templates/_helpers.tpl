{{- define "caplog.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "caplog.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: app
{{- end -}}

{{- define "caplog.postgresSelector" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: postgres
{{- end -}}

{{- define "caplog.labels" -}}
{{ include "caplog.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "caplog.dbSecret" -}}
{{- if .Values.database.enabled -}}
{{- .Values.database.auth.existingSecret | default (printf "%s-db" (include "caplog.fullname" .)) -}}
{{- else -}}
{{- required "externalDatabase.existingSecret is required when database.enabled is false" .Values.externalDatabase.existingSecret -}}
{{- end -}}
{{- end -}}
