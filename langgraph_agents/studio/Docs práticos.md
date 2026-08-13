# Docs práticos
## Interface
É necessário fazer login com sua conta do Keycloak. 
Se não possuir conta ou permissão para acessar o serviço, entre em contato com os administradores do serviço.

## Profiles
Permitem salvar configurações dos agentes - estrutura, prompts e servidores MCP.

## MCP Servers
Permite adicionar outras ferramentas para os agentes. 

URL deve ser pública ou interna ao cluster, no formato `http://example-service.namespace.svc.cluster.local:80/sse` ou `/mcp`, dependendo do tipo de transporte.

Botão `Test`, ao lado de Tools, permite verificar se há conexão com o servidor MCP e desabilitar ferramentas não desejadas - ferramentas desabilitadas não são passadas para o contexto do agente.

## Credenciais
Permite passar credenciais privadas para os servidores MCP, salvando-as no seu browser em vez dos profiles.

**Exemplo**: 
Criar credencial de nome `gh_key` e valor `gh-123456`.

No MCP Github, adicionar header:
- Header-Name: `Authorization`
- Value: Bearer `{{gh_key}}`
Quando o agente usar alguma tool deste MCP, será enviado o header `Authorization`: `Bearer gh-123456`.

## Agent Structure
Define estrutura entre: 
- Agente único, com acesso a todos os MCPs;
- Agente Orquestrador, com um sub-agente por MCP;
- 'Magentic': Agente Orquestrador guiado por uma lista de tarefas (task e progress ledgers), com um sub-agente por MCP;
- 'Tool Call': Agente Orquestrador, com um sub-agente por MCP, chama por tool call em vez de JSON na resposta.

Sub-agent Memory: 
- Stateful: sub-agentes mantém memória das últimas vezes que foram chamados na conversa;
- Stateless: sub-agentes não mantém memória entre chamadas.

## Prompts
System Prompts passadas aos agentes a cada chamada.

Para Single Agent, só há uma prompt. 
Para as demais estruturas, prompts são para cada um dos sub-agentes de acordo com o ID, e para o agente orquestrador, por padrão mostrando a resposta do último agente usando a variável `{last_agent_answer}`.


## Administração
### Provisionamento por usuário

Para cada novo usuário, devem ser seguidos três passos:

**1. Permitir ao Service Account k8s-mcp o uso do impersonate para esse usuário** (adicionar ao ClusterRole):
```bash
kubectl patch clusterrole k8s-mcp-impersonator --type=json \
  -p='[{"op":"add","path":"/rules/0/resourceNames/-","value":"<USERNAME>-readonly"}]'
```

**2. Permitir acesso de visualização ao namespace do usuário** (feito para cada namespace do usuário):
```bash
kubectl create rolebinding <USERNAME>-k8s-mcp-readonly \
  --clusterrole=view \
  --user=<USERNAME>-readonly \
  -n <NAMESPACE>
```

**3. No Keycloak:** criar usuário e adicionar ao grupo `k8s-agent-users`, para que tenha a role `acesso-k8s-agent`.

`<USERNAME>` deve ser o mesmo que `preferred_username`, no Keycloak. O sufixo `-readonly` é adicionado pelo Agent Studio automaticamente ao chamar o k8s-mcp.

Para padronizar: nome do usuário em referência deve ser `<preferred_username>-readonly` e o nome do rolebinding deve ser `<preferred_username>-k8s-mcp-readonly`.

## k8s-mcp
O k8s-mcp, que vem por padrão na interface, é um servidor MCP que permite que o agente acesse dados dos namespaces do usuário.
A aplicação manda o header `X-Remote-User`, no padrão `<preferred_username>-readonly`, para o MCP. 

Se este nome estiver no ClusterRole `k8s-mcp-impersonator`, o MCP poderá acessar informações dos namespaces que este 'usuário virtual' pode acessar, definido pelos RoleBindings `<preferred_username>-k8s-mcp-readonly`.

Por enquanto, a forma de proteção deste servidor MCP é que está em um namespace restrito. 
Caso seja publicado em algum momento, deve haver alguma forma de autenticação, como o Keycloak.