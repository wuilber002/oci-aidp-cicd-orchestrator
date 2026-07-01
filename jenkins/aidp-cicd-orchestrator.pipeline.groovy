def withGitLabStatus(String name, Closure body) {
  if (env.gitlabMergeRequestIid?.trim()) {
    gitlabCommitStatus(name) {
      body()
    }
  } else {
    body()
  }
}

pipeline {
  agent any
  options {
    disableConcurrentBuilds()
    gitLabConnection('gitlab-demo')
    gitlabBuilds(builds: ['prepare', 'bootstrap_venv', 'oci_api_key_auth', 'prepare_target', 'publish_target'])
  }
  environment {
    ORCHESTRATOR_REPO_BRANCH = 'main'
    GITLAB_CICD_USERNAME = 'cicd_aidp'
    GITLAB_ORCHESTRATOR_PROJECT_NAME = 'oci-aidp-cicd-orchestrator'
    ORCHESTRATOR_CONTEXT_PATH = 'contexts/demo.yaml'
  }
  stages {
    stage('prepare') {
      steps {
        script {
          withGitLabStatus('prepare') {
            withCredentials([usernamePassword(credentialsId: 'github-demo-pat', usernameVariable: 'GITHUB_USERNAME', passwordVariable: 'GITHUB_TOKEN')]) {
              sh '''
                set -euo pipefail
                rm -rf orchestrator
                export GIT_SSL_NO_VERIFY=true
                git clone \
                  --depth 1 \
                  --branch "$ORCHESTRATOR_REPO_BRANCH" \
                  "https://${GITHUB_USERNAME}:${GITHUB_TOKEN}@github.com/wuilber002/oci-aidp-cicd-orchestrator.git" \
                  orchestrator
              '''
            }
          }
        }
      }
    }
    stage('bootstrap_venv') {
      steps {
        script {
          withGitLabStatus('bootstrap_venv') {
            sh '''
              set -euo pipefail
              cd orchestrator
              python3 bootstrap_venv.py
            '''
          }
        }
      }
    }
    stage('oci_api_key_auth') {
      steps {
        script {
          withGitLabStatus('oci_api_key_auth') {
            sh '''
              set -euo pipefail
              cd orchestrator
              .venv/bin/python - <<'PY'
import base64
import json
import os
import shlex
from pathlib import Path

import yaml

context_path = Path(os.environ.get('ORCHESTRATOR_CONTEXT_PATH', 'contexts/demo.yaml'))
cfg = yaml.safe_load(context_path.read_text(encoding='utf-8')) or {}
runtime = cfg.get('runtime') or {}
auth_method = str(runtime.get('auth_method', '')).strip()
secret_id = str(runtime.get('secret_id', '')).strip()
region = str((cfg.get('aidp') or {}).get('region') or '').strip()
env_file = Path('.oci_env')

if auth_method != 'api_key' or not secret_id:
    env_file.write_text('export OCI_AUTH_MODE=disabled\n', encoding='utf-8')
    print(f'Skipping OCI API key auth stage: auth_method={auth_method!r}, secret_id_present={bool(secret_id)}')
    raise SystemExit(0)

import oci

signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
client = oci.secrets.SecretsClient(config={'region': region}, signer=signer)
bundle = client.get_secret_bundle(secret_id).data
payload = json.loads(base64.b64decode(bundle.secret_bundle_content.content).decode('utf-8'))
oci_dir = Path('.oci')
oci_dir.mkdir(exist_ok=True)
key_path = (oci_dir / 'api_key.pem').resolve()
config_path = (oci_dir / 'config').resolve()
key_path.write_text(payload['OCI_CLI_KEY_PEM'], encoding='utf-8')
key_path.chmod(0o600)
config_path.write_text(
    '\n'.join([
        '[DEFAULT]',
        f"user={payload['OCI_CLI_USER']}",
        f"fingerprint={payload['OCI_CLI_FINGERPRINT']}",
        f"tenancy={payload['OCI_CLI_TENANCY']}",
        f"region={payload['OCI_CLI_REGION']}",
        f"key_file={key_path}",
        ''
    ]),
    encoding='utf-8'
)
config_path.chmod(0o600)
env_values = {
    'OCI_AUTH_MODE': 'api_key',
    'OCI_CLI_REGION': payload['OCI_CLI_REGION'],
    'OCI_CLI_USER': payload['OCI_CLI_USER'],
    'OCI_CLI_FINGERPRINT': payload['OCI_CLI_FINGERPRINT'],
    'OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING': payload.get('OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING', 'True'),
    'OCI_CLI_TENANCY': payload['OCI_CLI_TENANCY'],
    'OCI_CLI_KEY_FILE': str(key_path),
    'OCI_CLI_CONFIG_FILE': str(config_path),
    'OCI_CONFIG_FILE': str(config_path),
    'OCI_PROFILE': 'DEFAULT',
}
env_file.write_text(''.join(f"export {k}={shlex.quote(v)}\n" for k, v in env_values.items()), encoding='utf-8')
env_file.chmod(0o600)
print(f'Prepared OCI API key auth from secret {secret_id}')
PY
            '''
          }
        }
      }
    }
    stage('prepare_target') {
      steps {
        script {
          withGitLabStatus('prepare_target') {
            sh '''
              set -euo pipefail
              cd orchestrator
              [ -f .oci_env ] && . ./.oci_env || true
              .venv/bin/python cicd_prepare.py target --context demo
            '''
          }
        }
      }
    }
    stage('publish_target') {
      steps {
        script {
          withGitLabStatus('publish_target') {
            sh '''
              set -euo pipefail
              cd orchestrator
              [ -f .oci_env ] && . ./.oci_env || true
              .venv/bin/python cicd_publish_target.py --context demo
            '''
          }
        }
      }
    }
  }
  post {
    success {
      script {
        if (env.gitlabMergeRequestIid?.trim()) {
          updateGitlabCommitStatus name: 'publish_target', state: 'success'
        }
      }
    }
    failure {
      script {
        if (env.gitlabMergeRequestIid?.trim()) {
          updateGitlabCommitStatus name: 'publish_target', state: 'failed'
        }
      }
    }
    aborted {
      script {
        if (env.gitlabMergeRequestIid?.trim()) {
          updateGitlabCommitStatus name: 'publish_target', state: 'canceled'
        }
      }
    }
  }
}
