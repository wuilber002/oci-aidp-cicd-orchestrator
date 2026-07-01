# Jenkins Orchestrator Job

[Leia este documento em Português (Brasil)](README.pt-BR.md)

This directory contains the required material to recreate the `aidp-cicd-orchestrator` job.

## Job

- Name: `aidp-cicd-orchestrator`
- Type: `Pipeline`
- Description: `Pipeline bootstrapped by the OCI demo stack for AIDP CI/CD merge requests.`
- Pipeline script: [aidp-cicd-orchestrator.pipeline.groovy](/home/inicoli/TRAMPO/OCI/AIDP/oci-aidp-cicd-orchestrator/make-infra/jenkins/aidp-cicd-orchestrator.pipeline.groovy)

## Trigger

The current flow uses the GitLab plugin in Jenkins, but the event is sent by GitHub Actions from the `oci-aidp-cicd-data` repository, simulating a GitLab `Push Hook`.

Relevant trigger parameters in the job:

- `triggerOnPush=true`
- `triggerOnMergeRequest=false`
- `branchFilterType=RegexBasedFilter`
- `sourceBranchRegex=^(.*/)?main$`
- `targetBranchRegex=.*`
- `cancelPendingBuildsOnUpdate=true`
- `cancelRunningBuildsOnUpdate=true`

Expected Jenkins endpoint:

- `/project/aidp-cicd-orchestrator`

The webhook token must not be versioned here. It must exist in the Jenkins job and in the GitHub secret `JENKINS_TRIGGER_TOKEN`.

## Jenkins Prerequisites

- Plugin `workflow-aggregator`
- Plugin `gitlab-plugin`
- GitLab connection: `gitlab-demo`
- Jenkins credential: `github-demo-pat`
  - type: `usernamePassword`
  - used to clone `https://github.com/wuilber002/oci-aidp-cicd-orchestrator.git`

## External Pipeline Dependencies

- Orchestrator repository:
  - `https://github.com/wuilber002/oci-aidp-cicd-orchestrator.git`
- Branch:
  - `main`
- Context loaded inside the orchestrator:
  - `contexts/demo.yaml`

## Expected Fields In `contexts/demo.yaml`

Minimum required structure for the current job:

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

## What The Job Does

1. Clones the `oci-aidp-cicd-orchestrator` repository
2. Runs `bootstrap_venv.py`
3. If `runtime.auth_method=api_key`, loads the secret from Vault and builds `.oci_env`
4. Runs `cicd_prepare.py target --context demo`
5. Runs `cicd_publish_target.py --context demo`

## Current Stages

- `prepare`
- `bootstrap_venv`
- `oci_api_key_auth`
- `prepare_target`
- `publish_target`
