# Pacote de Transporte de Workspaces CI/CD

[Read this document in English](README.md)

Pacote mínimo para executar o fluxo `workspace source -> bundle Git -> workspace target`.

## Aviso Legal

Este repositório contém scripts de automação, templates e materiais de referência em estilo comunitário, destinados a apoiar experimentação, demonstrações e cenários customizados de orquestração de CI/CD.

Esses artefatos não constituem funcionalidade oficial de produto Oracle, não são cobertos pelos serviços de suporte da Oracle e não possuem compromisso de nível de serviço, manutenção, evolução ou garantia de compatibilidade.

O uso dos scripts, pipelines, templates e exemplos deste repositório é de inteira responsabilidade e por conta e risco do usuário. Cabe ao usuário validar comportamento, segurança, conformidade e adequação operacional antes de utilizar qualquer conteúdo em ambientes de desenvolvimento, teste ou produção.

A Oracle não se responsabiliza por perdas, danos, interrupções de serviço, configurações incorretas ou impactos não intencionais decorrentes do uso, modificação ou redistribuição dos materiais contidos neste repositório.

## Arquivos

- `bootstrap_venv.py`: cria `.venv` e instala dependências a partir de `requirements.txt`.
- `cicd_prepare.py`: prepara `source-workspace.yaml` e `target-workspace.yaml`.
- `demo/cicd_seed.py`: cria artefatos versionáveis e recursos de exemplo no workspace source para demos e testes controlados.
- `cicd_publish_source.py`: recria o bundle de stage a partir do source e o versiona no Git.
- `cicd_publish_target.py`: consome o conteúdo do Git no target, sincroniza o bundle de deploy, faz o deploy e reconcilia nomes.
- `demo/cicd_destroy.py`: remove completamente os workspaces `source` e `target` usados apenas em demos e testes controlados do fluxo.
- `cicd_deploy.py`: executor de deploy do bundle no workspace target.
- `cicd-orchestrator.ini`: centraliza os parâmetros operacionais customizáveis do orquestrador.

## Direção Arquitetural

O projeto está organizado em torno de dois casos de uso:

- `source publish`: prepara o `bundle_stage` no workspace source e publica esse conteúdo no Git.
- `target publish`: consome o conteúdo do Git, sincroniza o `bundle/` de deploy no workspace target, faz o deploy e reconcilia nomes dos recursos.

Essa estrutura mantém a lógica compartilhada em módulos internos e expõe scripts separados apenas onde o caso de uso realmente muda.

## Pré-requisitos

- Python 3.10+.
- `oci`, `requests`, `PyYAML`.
- acesso HTTP ao release oficial do SDK AIDP no GitHub. O bootstrap baixa automaticamente o `.whl` mais recente e instala esse client na `.venv`.
- autenticação OCI por variáveis de ambiente:

```bash
export OCI_CLI_TENANCY=...
export OCI_CLI_USER=...
export OCI_CLI_FINGERPRINT=...
export OCI_CLI_KEY_FILE=...
export OCI_CLI_REGION=us-ashburn-1
```

## Versionamento Seguro

Arquivos locais com valores reais do seu ambiente não devem ir para o Git, por exemplo:

- `source-workspace.yaml`
- `target-workspace.yaml`
- `commands.sh`
- `logs/`
- o cache local `.downloads/` com o `.whl` mais recente baixado do release oficial

Os arquivos `source-workspace.yaml` e `target-workspace.yaml` são gerados pelos scripts com base no `cicd-orchestrator.ini` e nos parâmetros informados pelo usuário. Por isso, devem ser tratados como artefatos locais do ambiente, e não como arquivos de exemplo versionados.

Os exemplos de execução dos scripts devem ficar neste `README`.

## Autenticação

Os scripts aceitam estes valores em `--auth-method`:

- `api_key`
- `instance_principal`
- `resource_principal`
- `oke_workload_identity`

Quando `--auth-method` não é informado, o valor default vem de `cicd-orchestrator.ini`.

Uso típico:

- `api_key`: execução local, notebook pessoal ou automação simples fora da OCI.
- `instance_principal`: execução dentro de uma VM OCI com políticas IAM adequadas.
- `resource_principal`: execução em serviços OCI que expõem resource principal.
- `oke_workload_identity`: execução dentro de workloads no OKE com workload identity configurada.

Exemplos rápidos:

```bash
python cicd_publish_target.py --auth-method api_key
python cicd_publish_target.py --auth-method instance_principal
python cicd_publish_target.py --auth-method resource_principal
python cicd_publish_target.py --auth-method oke_workload_identity
```

## Setup Inicial

```bash
python3 bootstrap_venv.py
source .venv/bin/activate
```

O bootstrap baixa automaticamente o `.whl` mais recente do release oficial:

```bash
python3 bootstrap_venv.py
```

Se quiser forçar um arquivo específico já presente no diretório do projeto:

```bash
python3 bootstrap_venv.py .downloads/aidp_python_client-1.0.2-py3-none-any.whl
```

## Fluxos de Uso

### Fluxo real de produção

No processo real de produção, o orquestrador não deve depender do workspace source. O desenvolvedor é responsável por:

1. preparar o conteúdo no workspace source
2. gerar o bundle a ser transportado
3. publicar esse conteúdo no repositório Git
4. abrir o merge request que dispara a pipeline

Na pipeline, o processo correto deve interagir apenas com o workspace target:

```text
Workspace do desenvolvedor
    |
    |  geração manual do bundle + publicação no Git
    v
Repositório Git
    |
    |  pipeline de CI/CD
    v
Workspace target
    |
    `-> sincronizar bundle de deploy -> deploy -> reconciliar nomes dos recursos
```

Fluxo operacional esperado:

```text
prepare target
    -> publish target
```

Mapeamento de scripts:

```text
cicd_prepare.py target
    -> cicd_publish_target.py
```

### Fluxo de demo ponta a ponta

O fluxo de demo existe para validar todo o processo ponta a ponta, inclusive a parte que, em produção, ficaria sob responsabilidade do desenvolvedor no workspace source.

Ele reaproveita a mesma etapa de publish no target usada no fluxo real:

```text
demo destroy
    -> prepare source
    -> prepare target
    -> demo seed
    -> publish source
    -> publish target
```

Visualmente:

```text
Workspace demo source
    |
    |  demo/cicd_seed.py
    v
publish source
    |
    |  cria bundle_stage + publica no Git
    v
Repositório Git
    |
    |  mesma lógica de release do target usada em produção
    v
publish target
    |
    `-> sincronizar bundle de deploy -> deploy -> reconciliar nomes dos recursos
```

Mapeamento de scripts:

```text
demo/cicd_destroy.py
    -> cicd_prepare.py source
    -> cicd_prepare.py target
    -> demo/cicd_seed.py
    -> cicd_publish_source.py
    -> cicd_publish_target.py
```

### Exemplo objetivo de demo ponta a ponta

```bash
python demo/cicd_destroy.py --auth-method api_key --yes

python cicd_prepare.py --auth-method api_key source \
  --region us-ashburn-1 \
  --aidp-ocid <AIDP_OCID> \
  --workspace-name developer \
  --repository-url <REPO_URL> \
  --credential-name <AIDP_GIT_CREDENTIAL_NAME>

python cicd_prepare.py --auth-method api_key target \
  --workspace-name production

python demo/cicd_seed.py --auth-method api_key

python cicd_publish_source.py \
  --auth-method api_key \
  --commit-message "chore: publish developer workspace transport bundle"

python cicd_publish_target.py \
  --auth-method api_key \
  --commit-message "chore: publish transport bundle into production workspace"
```

Preparar o workspace source:

```bash
python cicd_prepare.py --auth-method api_key source \
  --region us-ashburn-1 \
  --aidp-ocid <AIDP_OCID> \
  --workspace-name developer \
  --repository-url <REPO_URL> \
  --branch main \
  --parent-dir /Workspace/cicd \
  --bundle-name bundle \
  --credential-name <AIDP_GIT_CREDENTIAL_NAME>
```

Preparar o workspace target:

```bash
python cicd_prepare.py --auth-method api_key target \
  --workspace-name production
```

## Seed de Demo

```bash
python demo/cicd_seed.py --auth-method api_key
```

O seed cria:

- notebooks em `src/`
- um arquivo em `shared/`
- um cluster real no workspace
- dois jobs reais no workspace, apontando para os notebooks criados

Importante:

- o seed não cria `bundle/` como pasta comum
- o caminho `bundle/` precisa ser materializado pelo `cicd_publish_target.py` como recurso de bundle
- se `bundle/` já existir como folder comum no workspace, remova-o antes de validar o fluxo

## Publish Source

Por padrão, o script usa `./source-workspace.yaml` e `./target-workspace.yaml`.
Os parâmetros operacionais compartilhados ficam em `./cicd-orchestrator.ini`.

Precedência de configuração:

- argumento de CLI
- YAML do workspace (`source-workspace.yaml` ou `target-workspace.yaml`)
- `cicd-orchestrator.ini`
- fallback interno do código

```bash
python cicd_publish_source.py \
  --auth-method api_key \
  --commit-message "chore: publish developer workspace transport bundle"
```

## Publish Target

O publish no target consome o último conteúdo versionado no Git para concluir o deploy e a reconciliação.

```bash
python cicd_publish_target.py \
  --auth-method api_key \
  --commit-message "chore: publish transport bundle into production workspace"
```

## Prepare Target

No target, o `prepare` garante apenas o workspace, o diretório base e a git folder.
A criação ou recriação da shell do bundle de deploy em `bundle/` agora é responsabilidade do `cicd_publish_target.py`, que trata esse recurso de forma idempotente sempre que precisa publicar e fazer deploy.

## Destroy

Remove completamente os workspaces `source` e `target` definidos nos YAMLs gerados pelo `prepare`.
Esse script é exclusivo para demos e testes controlados; ele não faz parte do fluxo normal de CI/CD em produção:

```bash
python demo/cicd_destroy.py --auth-method api_key --yes
```

Sem `--yes`, o script mostra os nomes e keys dos workspaces que serão removidos e pede confirmação interativa.
Com `--yes`, ele mostra o mesmo aviso e espera 5 segundos antes de iniciar a destruição.

Para destruir apenas um lado:

```bash
python demo/cicd_destroy.py --auth-method api_key --yes --skip-target
python demo/cicd_destroy.py --auth-method api_key --yes --skip-source
```

## Observações

- O bundle de stage para transporte fica em `bundle_stage/` dentro da git folder.
- O bundle canônico de deploy fica em `bundle/`.
- No source, o `cicd_publish_source.py` sempre recria o mesmo `bundle_stage/`; se existir uma versão anterior, ela é removida antes da nova criação.
- O Git deve sempre transportar o `bundle_stage/`; o `bundle/` do target é mantido como bundle de deploy.
- No target, o script preserva a raiz do `bundle/`, a pasta `.aidp` e tolera um `.gitignore` local; ele remove apenas o restante do conteúdo interno e copia para dentro dele o conteúdo vindo de `bundle_stage/`, sem sobrescrever o `.aidp` local.
- O `cicd_publish_target.py` garante a shell do bundle de deploy no target quando necessário; o `prepare target` não precisa mais materializar esse recurso antecipadamente.
- A classificação de pastas não deve depender apenas de `type/resourceType`: o script usa metadata Git para `git_folder` e a presença de `aidp_workbench.yaml` e `.aidp/resource_origins.yaml` para `bundle_folder`.
- Para inspecionar o payload bruto e a classificação calculada de um caminho, use `python tools/cicd_inspect_object.py --auth-method api_key --config target-workspace.yaml --path /Workspace/...`.
- No seed, o cluster pode terminar em `ACTIVE` ou `STOPPED`; `STOPPED` é suficiente para manter o recurso transportável e referenciável pelos jobs.
- Se a validação manual na console exigir executar notebooks ou jobs imediatamente, inicie o cluster manualmente quando ele estiver em `STOPPED`.
- O caminho `bundle/` não deve ser pré-criado por seed ou manualmente como folder comum; caso contrário, o AIDP pode preservar o destino como `Folder` em vez de `Bundle`.
- O commit/push do `cicd_publish_source.py` é feito via API Git do próprio AIDP (`commitPush`), e não por `git` local no host.
- O deploy do bundle usa o endpoint `dataLakes/20240831/.../bundles/actions/deploy`, que foi validado manualmente via console.
- Se o repositório remoto estiver vazio, o fluxo continua válido, mas o source precisa ter sua git folder criada primeiro pelo `cicd_prepare.py`.
- A retenção de logs e os demais parâmetros operacionais ficam centralizados em [cicd-orchestrator.ini](/home/inicoli/TRAMPO/OCI/AIDP/oci-aidp-cicd-orchestrator/cicd-orchestrator.ini); o carregamento e o fallback desses defaults ficam em [core/settings.py](/home/inicoli/TRAMPO/OCI/AIDP/oci-aidp-cicd-orchestrator/core/settings.py).
- Os métodos de autenticação suportados são `api_key`, `instance_principal`, `resource_principal` e `oke_workload_identity`; informe `--auth-method` explicitamente quando necessário.
