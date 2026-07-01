# Jenkins Orchestrator Job

[Read this document in English](README.md)

Esta pasta concentra o necessario para recriar o job `aidp-cicd-orchestrator`.

## Job

- Nome: `aidp-cicd-orchestrator`
- Tipo: `Pipeline`
- Descricao: `Pipeline bootstrapped by the OCI demo stack for AIDP CI/CD merge requests.`
- Script do pipeline: [aidp-cicd-orchestrator.pipeline.groovy](/home/inicoli/TRAMPO/OCI/AIDP/oci-aidp-cicd-orchestrator/make-infra/jenkins/aidp-cicd-orchestrator.pipeline.groovy)

## Trigger

O fluxo atual usa o plugin GitLab no Jenkins, mas o evento chega a partir do GitHub Actions do repo `oci-aidp-cicd-data`, simulando um `Push Hook` do GitLab.

Parametros relevantes do trigger no job:

- `triggerOnPush=true`
- `triggerOnMergeRequest=false`
- `branchFilterType=RegexBasedFilter`
- `sourceBranchRegex=^(.*/)?main$`
- `targetBranchRegex=.*`
- `cancelPendingBuildsOnUpdate=true`
- `cancelRunningBuildsOnUpdate=true`

Endpoint esperado no Jenkins:

- `/project/aidp-cicd-orchestrator`

O token do webhook nao deve ser versionado aqui. Ele precisa existir no Jenkins/job e no secret `JENKINS_TRIGGER_TOKEN` do GitHub.

## Prerequisitos no Jenkins

- Plugin `workflow-aggregator`
- Plugin `gitlab-plugin`
- Connection GitLab: `gitlab-demo`
- Credencial Jenkins: `github-demo-pat`
  - tipo: `usernamePassword`
  - usado para clonar `https://github.com/wuilber002/oci-aidp-cicd-orchestrator.git`

## Dependencias externas do pipeline

- Repo do orchestrator:
  - `https://github.com/wuilber002/oci-aidp-cicd-orchestrator.git`
- Branch:
  - `main`
- Contexto lido dentro do orchestrator:
  - `contexts/demo.yaml`

## Campos esperados em `contexts/demo.yaml`

Minimo necessario para o job atual:

```yaml
runtime:
  auth_method: api_key
  secret_id: <vault-secret-ocid>

aidp:
  ocid: <aidp-ocid>
  region: sa-saopaulo-1

git:
  repository_url: https://github.com/wuilber002/oci-aidp-cicd-data.git
  credential_name: demo_cicd
  branch: main
  parent_dir: /Workspace/demo_cicd
  bundle_path: bundle
  stage_bundle_path: bundle_stage
```

## O que o job faz

1. Clona o repo `oci-aidp-cicd-orchestrator`
2. Executa `bootstrap_venv.py`
3. Se `runtime.auth_method=api_key`, busca o secret no Vault e monta `.oci_env`
4. Executa `cicd_prepare.py target --context demo`
5. Executa `cicd_publish_target.py --context demo`

## Stages atuais

- `prepare`
- `bootstrap_venv`
- `oci_api_key_auth`
- `prepare_target`
- `publish_target`
